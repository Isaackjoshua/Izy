"""What happens on every tick, with no Qt in it.

This is the whole policy of the running daemon — poll, record, classify, judge
drift, fire reminders — expressed as plain Python over an injected clock. It
owns the SQLite connection and every collaborator; `worker.py` is reduced to
Qt plumbing that calls `tick()` and turns the returned events into signals.

The split exists so this logic can be tested at all. Anything living inside a
QObject on a worker thread needs an event loop and a running thread to exercise;
here a full day of daemon behaviour replays in a unit test in milliseconds.

Nothing in this module imports Qt, and nothing in it may.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from . import db
from .budget import InterruptionBudget
from .classifier import Classifier
from .drift import DriftDetector
from .ipc import StateSnapshot
from .llm import LLM
from .models import to_iso, utcnow
from .reminders import ReminderScheduler, parse as parse_reminder
from .reminders import store as reminder_store
from .selflabel import SelfLabelPrompt
from .sessions import Phase, SessionManager
from .tracker import Tracker
from .watchers import pick_watcher

log = logging.getLogger(__name__)

# Event kinds handed back to whatever is driving the pipeline.
PHASE = "phase"                 # (phase_value, Session|None)
SELF_LABEL = "self_label"       # (event_id, app, title)
REMINDER = "reminder"           # (reminder_id, text)
CONFIRM_REMINDER = "confirm"    # (raw_text,)
ASK_ON_TASK = "ask_on_task"     # (event_id, intent, what)
DRIFT = "drift"                 # (intervention_id, message)
MASCOT = "mascot"               # (state,) — neutral | soft-alert | asleep
ASK_OUTCOME = "ask_outcome"     # (session_id, intent)
STATUS = "status"               # (message,)


@dataclass(frozen=True)
class Event:
    kind: str
    payload: tuple

    def __iter__(self):
        return iter((self.kind, self.payload))


class Pipeline:
    #: How often to re-read session state written by `izy start` / `izy stop`
    #: in another process. Cheap, and bounds how much activity a CLI-started
    #: session can lose to a NULL session_id.
    RESYNC_EVERY_S = 3.0

    def __init__(self, cfg, db_path=None, *, clock=utcnow, watcher=None,
                 conn=None, llm=None, command_queue=None, state_bus=None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.conn = conn if conn is not None else db.connect(db_path)
        self.watcher = watcher if watcher is not None else pick_watcher(cfg.watcher)
        # The control plane (izy-v2.md §1). Both optional: the daemon supplies
        # them, tests and headless runs leave them None and the tick behaves
        # exactly as before — no command draining, no publishing.
        self.command_queue = command_queue
        self.state_bus = state_bus

        self.tracker = Tracker(
            self.conn,
            flush_interval_s=cfg.watcher.flush_interval_s,
            stale_after_s=max(60.0, cfg.watcher.poll_interval_s * 30),
            clock=clock)
        self.sessions = SessionManager(self.conn, cfg, clock=clock)
        self.selflabel = SelfLabelPrompt(self.conn, cfg, clock=clock)
        self.budget = InterruptionBudget(self.conn, cfg, clock=clock)
        self.llm = llm if llm is not None else LLM(self.conn, cfg, clock=clock)
        self.reminders = ReminderScheduler(self.conn, cfg, clock=clock)
        self.classifier = Classifier(self.conn, cfg, self.llm, clock=clock)
        self.drift = DriftDetector(self.conn, cfg, self.budget, clock=clock)

        self._out: list[Event] = []
        self._last_resync = None
        self._last_eod_check = None
        self._last_report_day = None
        self._last_phase = None
        self._last_classified_id = 0
        self._seen_apps: set[str] = set()
        self._mascot_state = None
        self._tick_count = 0
        self._last_snap = None          # last Snapshot the watcher returned

        self.sessions.on_change(self._on_phase_change)

    # --- lifecycle ---------------------------------------------------------

    def describe(self) -> str:
        describe = getattr(self.watcher, "describe", None)
        return describe() if describe else self.watcher.name

    def start(self) -> list[Event]:
        self.sessions.recover()
        self._on_phase_change(self.sessions.phase, self.sessions.current)
        return self._drain()

    def stop(self) -> None:
        try:
            self._flush_classifier(force=True)
        except Exception:
            log.exception("final classifier flush failed")
        self.tracker.stop()
        self.watcher.close()
        self.conn.close()

    # --- the tick ----------------------------------------------------------

    def tick(self) -> list[Event]:
        """One poll's worth of work. Returns what the UI was told this tick.

        Tick order (izy-v2.md §2 will insert the arbiter; Phase 1 only adds the
        command drain at the front and state publication at the end):

          1  drain_commands   — run API/CLI requests on the single writer thread
             maybe_resync     — reconcile CLI-direct DB writes (the old path)
          2  poll watcher
          3  record span
          4  maybe_overrun / self_label / reminders / classify / drift
          5  update_mascot
          6  publish_state    — hand clients an immutable snapshot
        """
        self._tick_count += 1
        self._drain_commands()
        self._maybe_resync()

        try:
            snap = self.watcher.poll()
        except Exception:
            log.exception("watcher poll failed")
            snap = None
        self._last_snap = snap
        try:
            self.tracker.tick(snap)
        except Exception:
            log.exception("tracker tick failed")

        if self.sessions.is_overrun():
            self._maybe_overrun()
        self._maybe_self_label()
        self._check_reminders(snap)
        self._classify_new_events()
        self._check_drift()
        self._update_mascot()
        self._publish_state()
        return self._drain()

    def _drain(self) -> list[Event]:
        out, self._out = self._out, []
        return out

    def _emit(self, kind: str, *payload) -> None:
        self._out.append(Event(kind, payload))

    # --- control plane -----------------------------------------------------

    #: Command name -> the Pipeline method it dispatches to. This is the entire
    #: set of mutations the API and CLI can ask for; nothing outside it can
    #: reach the writer. Methods that return a value have it delivered back
    #: through the command's Future.
    _COMMANDS = {
        "start_session": "start_session",
        "end_session": "end_session",
        "record_outcome": "record_outcome",
        "add_reminder": "add_reminder",
        "reminder_done": "reminder_done",
        "reminder_snooze": "reminder_snooze",
        "reminder_dismiss": "reminder_dismiss",
        "record_self_label": "record_self_label",
        "skip_self_label": "skip_self_label",
        "record_on_task_answer": "record_on_task_answer",
        "record_drift_response": "record_drift_response",
    }

    def _drain_commands(self) -> None:
        """Run every queued command on the tick thread — the one writer.

        Each command's Future gets the method's return value, or the exception,
        so an HTTP handler on another thread can wait for a real result without
        ever touching the DB itself. A bad command name fails its own Future and
        does not disturb the tick.
        """
        if self.command_queue is None:
            return
        for cmd in self.command_queue.drain():
            method = self._COMMANDS.get(cmd.name)
            try:
                if method is None:
                    raise ValueError(f"unknown command: {cmd.name}")
                result = getattr(self, method)(**cmd.args)
                if not cmd.future.done():
                    cmd.future.set_result(result)
            except Exception as e:
                log.exception("command %s failed", cmd.name)
                if not cmd.future.done():
                    cmd.future.set_exception(e)

    def _publish_state(self) -> None:
        if self.state_bus is None:
            return
        try:
            self.state_bus.publish(self.snapshot())
        except Exception:
            log.exception("state publish failed")

    def snapshot(self) -> StateSnapshot:
        """Build the immutable picture clients read. Pure reads; safe to call
        from the tick or a test."""
        phase = self.sessions.phase
        session = self.sessions.current
        session_dict = None
        if session and session.is_open:
            remaining = self.sessions.remaining()
            elapsed = (self.clock() - session.started_at).total_seconds()
            session_dict = {
                "id": session.id,
                "intent": session.declared_intent,
                "planned_minutes": session.planned_minutes,
                "elapsed_s": int(max(0, elapsed)),
                "remaining_s": int(remaining.total_seconds()) if remaining else 0,
            }
        snap = self._last_snap
        return StateSnapshot(
            tick=self._tick_count,
            ts=to_iso(self.clock()),
            mascot=self.mascot_state(),
            phase=phase.value,
            watcher=self.describe(),
            session=session_dict,
            focus_app=(snap.app if snap else None),
            focus_title=(snap.title if snap else None),
            counters=self._today_counters(),
            connected=True,
        )

    def _today_counters(self) -> dict:
        """Today's headline numbers, read straight from the DB so they survive a
        restart. Kept cheap: a handful of indexed aggregates once per tick."""
        try:
            day = self.clock()
            lo, hi = db.day_bounds(day)
            on = off = afk = 0.0
            rows = self.conn.execute(
                "SELECT e.duration_s, e.afk,"
                " (SELECT l.on_task FROM labels l WHERE l.event_id = e.id"
                "  ORDER BY l.id DESC LIMIT 1) AS on_task"
                " FROM activity_events e WHERE e.ts >= ? AND e.ts < ?", (lo, hi)
            ).fetchall()
            for r in rows:
                if r["afk"]:
                    afk += r["duration_s"] or 0
                elif r["on_task"] == 1:
                    on += r["duration_s"] or 0
                elif r["on_task"] == 0:
                    off += r["duration_s"] or 0
            sessions = self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE started_at >= ? AND started_at < ?",
                (lo, hi)).fetchone()[0]
            calls, cost = self.llm.spend_today()
            return {"on_task_s": int(on), "off_task_s": int(off), "afk_s": int(afk),
                    "sessions": sessions, "tier3_calls": calls,
                    "tier3_cost_usd": round(cost, 4)}
        except Exception:
            log.exception("counter build failed")
            return {}

    # --- mascot ------------------------------------------------------------

    def mascot_state(self) -> str:
        """asleep off a session, soft-alert while drifting, neutral otherwise.

        Derived every tick rather than set at the moment of an alert. Setting it
        on the alert alone left no way back: the mascot went orange when you
        drifted and stayed orange, because nothing was watching for you
        returning to the task.
        """
        session = self.sessions.current
        if not session or self.sessions.phase is not Phase.FOCUS:
            return "asleep"
        state = self.drift.state(session.id)
        if self.budget.drift_qualifies(state.off_task_minutes):
            return "soft-alert"
        return "neutral"

    def _update_mascot(self) -> None:
        try:
            state = self.mascot_state()
        except Exception:
            log.exception("mascot state failed")
            return
        if state != self._mascot_state:
            self._mascot_state = state
            self._emit(MASCOT, state)

    # --- sessions ----------------------------------------------------------

    def _maybe_resync(self) -> None:
        now = self.clock()
        if self._last_resync is not None and \
                (now - self._last_resync).total_seconds() < self.RESYNC_EVERY_S:
            return
        self._last_resync = now
        try:
            self.sessions.resync()
        except Exception:
            log.exception("session resync failed")

    def _on_phase_change(self, phase, session) -> None:
        self.tracker.set_session(session.id if session else None)
        old, self._last_phase = self._last_phase, phase
        if old is not None:
            try:
                for context in self.reminders.contexts_for_phase_change(old, phase):
                    for r in self.reminders.on_context(context):
                        self._fire(r)
                # Leaving a focus session is a natural boundary: release
                # anything that came due while we were protecting the focus.
                if old is Phase.FOCUS and phase is not Phase.FOCUS:
                    for r in self.reminders.on_boundary(phase):
                        self._fire(r)
            except Exception:
                log.exception("reminder phase hook failed")
        self._emit(PHASE, phase.value, session)
        self._update_mascot()

    def _maybe_overrun(self) -> None:
        """The planned time is up. SPEC.md Feature 1: session end asks how it
        went — finished / partly / no.

        The question still goes through the interruption budget, so a day that
        has already spent its allowance ends the session quietly instead and
        the outcome is asked the next time you click the mascot. Ending it is
        not conditional on being allowed to ask.
        """
        session = self.sessions.current
        allowed, reason = self.budget.check("session_overrun")
        self._flush_classifier(force=True)
        self.sessions.end(None)
        if not allowed:
            log.debug("outcome prompt suppressed: %s", reason)
            return
        self.budget.record("session_overrun", session.declared_intent)
        self._emit(ASK_OUTCOME, session.id, session.declared_intent)

    # --- self-label --------------------------------------------------------

    def _maybe_self_label(self) -> None:
        if not self.selflabel.due(self.sessions.phase is Phase.BREAK):
            return
        allowed, reason = self.budget.check("self_label")
        if not allowed:
            log.debug("self-label suppressed: %s", reason)
            # Charge the slot anyway so suppressed prompts do not queue up and
            # fire in a burst the moment the budget frees.
            self.selflabel.last_asked = self.clock()
            return
        row = self.selflabel.pick_event()
        if row is None:
            self.selflabel.mark_asked()
            return
        # Mark the slot used *before* emitting. This is the whole fix: the emit
        # path used to leave last_asked untouched, so due() stayed true and the
        # prompt re-fired every tick. The dismissal/answer is recorded when the
        # user responds (skip/record), so it is not recorded again here.
        self.selflabel.mark_asked()
        self._emit(SELF_LABEL, row["id"], row["app"] or "", row["window_title"] or "")

    # --- classification ----------------------------------------------------

    def _classify_new_events(self) -> None:
        """Judge spans that have closed since the last tick.

        Only closed spans: an open span's duration is still growing, and judging
        it early would both misreport its length and waste the one cached
        verdict it is entitled to.
        """
        try:
            open_id = self.tracker.span.event_id if self.tracker.span else None
            rows = self.conn.execute(
                "SELECT e.*, s.declared_intent FROM activity_events e"
                " LEFT JOIN sessions s ON s.id = e.session_id"
                " WHERE e.id > ? AND (? IS NULL OR e.id != ?) ORDER BY e.id",
                (self._last_classified_id, open_id, open_id)).fetchall()
            for row in rows:
                self._last_classified_id = max(self._last_classified_id, row["id"])
                self.classifier.consider(row, row["declared_intent"])
            if self.classifier.batch_ready():
                self._flush_classifier()
        except Exception:
            log.exception("classification failed")

    def _flush_classifier(self, *, force: bool = False) -> None:
        decisions, ask = self.classifier.flush(force=force)
        if decisions:
            log.debug("classified %d event(s) via tier 3", len(decisions))
        for pending in ask:
            # Tier 4. Asking is free and honest; guessing is neither.
            allowed, reason = self.budget.check("self_label")
            if not allowed:
                log.debug("tier 4 question suppressed: %s", reason)
                break
            self.budget.record("self_label", pending.title)
            self._emit(ASK_ON_TASK, pending.event_id, pending.intent,
                       pending.title or pending.app or "that window")

    def _check_drift(self) -> None:
        try:
            session = self.sessions.current
            if not session:
                return
            result = self.drift.check(session.id, session.declared_intent)
            if result:
                self._emit(DRIFT, result[1], result[0])
        except Exception:
            log.exception("drift check failed")

    # --- reminders ---------------------------------------------------------

    def _check_reminders(self, snap) -> None:
        """Time reminders, app_opened triggers, and end_of_day. Phase changes
        are handled in _on_phase_change, the only place that knows a boundary
        was actually crossed."""
        try:
            for r in self.reminders.due_now(self.sessions.phase):
                self._fire(r)

            if snap is not None and snap.app:
                app = snap.app.lower()
                if app not in self._seen_apps:
                    self._seen_apps.add(app)
                    for r in self.reminders.on_context(f"app_opened:{app}"):
                        self._fire(r)

            if self.reminders.end_of_day_due(self._last_eod_check):
                self._last_eod_check = self.clock()
                for r in self.reminders.on_context("end_of_day"):
                    self._fire(r)
                self._write_retrospective()
        except Exception:
            log.exception("reminder check failed")

    def _fire(self, reminder) -> None:
        self.reminders.fired(reminder.id)
        self._emit(REMINDER, reminder.id, reminder.text)

    def _write_retrospective(self) -> None:
        """SPEC.md Feature 5: regenerated automatically at end of day.

        Writing the file is all that happens — nothing is opened and nothing is
        announced. The retrospective is for when you go looking.
        """
        today = self.clock().astimezone().date()
        if self._last_report_day == today:
            return
        self._last_report_day = today
        try:
            from . import report
            report.write(self.conn, self.cfg)
        except Exception:
            log.exception("end-of-day retrospective failed")

    # --- things the UI asks for --------------------------------------------

    def start_session(self, intent: str, minutes: int) -> list[Event]:
        try:
            self.sessions.start(intent, minutes)
        except ValueError as e:
            self._emit(STATUS, str(e))
        return self._drain()

    def end_session(self, outcome: str | None) -> list[Event]:
        # Settle anything buffered before the intent goes away — afterwards
        # there is nothing left to judge those windows against.
        self._flush_classifier(force=True)
        self.sessions.end(outcome or None)
        return self._drain()

    def record_outcome(self, session_id: int | None, outcome: str) -> None:
        """Answer the outcome question after the fact — by the time it is
        answered the session is already closed. With session_id None (the API
        path), apply it to the most recent session."""
        if session_id is None:
            latest = db.latest_session(self.conn)
            if latest is None:
                log.warning("record_outcome: no session to apply %r to", outcome)
                return
            session_id = latest.id
        try:
            self.sessions.record_outcome(session_id, outcome)
        except ValueError as e:
            log.warning("ignoring bad outcome: %s", e)

    def record_self_label(self, event_id: int, on_task: bool) -> None:
        self.selflabel.record(event_id, on_task)

    def skip_self_label(self) -> None:
        self.selflabel.skip()

    def record_on_task_answer(self, event_id: int, on_task: bool) -> None:
        self.classifier.record_user_answer(event_id, on_task)

    def record_drift_response(self, intervention_id: int, response: str) -> None:
        """A dismissal starts the 15-minute cooldown, so this must be recorded."""
        self.budget.resolve(intervention_id, response)

    def add_reminder(self, raw: str) -> list[Event]:
        """Parse and store. Asks rather than guessing when nothing is readable —
        SPEC.md forbids inventing a time that was not stated."""
        try:
            parsed = parse_reminder(raw, self.llm)
        except Exception:
            log.exception("reminder parse failed")
            self._emit(CONFIRM_REMINDER, raw)
            return self._drain()
        if not parsed.is_valid():
            self._emit(CONFIRM_REMINDER, raw)
            return self._drain()
        if parsed.urgent:
            parsed = replace(parsed, text=f"[urgent] {parsed.text}")
        r = reminder_store.add(self.conn, parsed, now=self.clock())
        when = (r.due_at.astimezone().strftime("%H:%M") if r.due_at
                else r.trigger_context)
        self._emit(STATUS, f"reminder saved for {when}")
        return self._drain()

    def reminder_done(self, reminder_id: int) -> None:
        self.reminders.done(reminder_id)

    def reminder_snooze(self, reminder_id: int) -> None:
        self.reminders.snooze(reminder_id)

    def reminder_dismiss(self, reminder_id: int) -> None:
        self.reminders.dismiss(reminder_id)
