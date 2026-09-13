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
from .interrupts import Arbiter, Context, Request
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
                 conn=None, llm=None, command_queue=None, state_bus=None,
                 fullscreen=None) -> None:
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

        # The single dispatch point (izy-v2.md §2). Its caps and cooldown read
        # the durable interrupt_log, so a restart does not hand out a fresh
        # allowance; every decision is logged back to the same table.
        self.arbiter = Arbiter(
            cfg,
            log_fn=lambda kind, pri, key, req_at, verdict, reason, now:
                db.log_interrupt(self.conn, kind, pri, key, req_at, verdict,
                                 reason, now=now),
            shown_since=lambda kind, since:
                db.interrupts_shown_since(self.conn, kind, since),
            last_shown=lambda: db.last_interrupt_shown(self.conn))
        #: Best-effort fullscreen/presentation detector. There is no unprivileged
        #: Wayland API for this, so the default says "no" and it is injectable —
        #: real detection would need the shell extension (a later phase). The
        #: gate exists and is tested; only the sensing is stubbed.
        self._fullscreen = fullscreen or (lambda: False)

        self._out: list[Event] = []
        self._last_resync = None
        self._last_eod_check = None
        self._last_report_day = None
        self._last_phase = None
        self._last_classified_id = 0
        self._last_suggest_label_id = 0
        self._last_q2_scan = None
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
            self._maybe_overrun()             # step 4  -> submit
        self._maybe_self_label()              # step 6  -> submit
        self._check_reminders(snap)           # step 7  -> submit
        self._classify_new_events()           # step 9  (may submit tier-4)
        self._collect_hint_suggestions()      # step 9b task hint suggestions
        self._maybe_q2_nudge()                # step 9c neglected-Q2 nudge
        self._check_drift()                   # step 10 -> submit
        self._dispatch_interrupts(snap)       # step 11 <- the only place we show
        self._update_mascot()                 # step 12
        self._publish_state()                 # step 13
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
        "create_task": "create_task",
        "update_task": "update_task",
        "set_task_quadrant": "set_task_quadrant",
        "reorder_task": "reorder_task",
        "delete_task": "delete_task",
        "accept_hint": "accept_hint",
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

    # --- the interrupt arbiter (step 11) -----------------------------------

    def _arbiter_context(self, snap) -> Context:
        session = self.sessions.current
        deep = 0.0
        if session and self.sessions.phase is Phase.FOCUS:
            try:
                deep = self.drift.state(session.id).deep_work_minutes
            except Exception:
                deep = 0.0
        fullscreen = False
        try:
            fullscreen = bool(self._fullscreen())
        except Exception:
            pass
        return Context(now=self.clock(), phase=self.sessions.phase.value,
                       afk=bool(snap.afk) if snap else False,
                       deep_work_minutes=deep, fullscreen=fullscreen)

    def _dispatch_interrupts(self, snap) -> None:
        """The one place anything reaches the screen. Everything submitted this
        tick (and everything still held) is arbitrated; at most one Event is
        emitted, and only for the request the arbiter chose to SHOW."""
        try:
            shown = self.arbiter.dispatch(self._arbiter_context(snap))
        except Exception:
            log.exception("arbiter dispatch failed")
            return
        if shown is None:
            return
        p = shown.payload
        event = p.get("event")
        if event == REMINDER:
            # Only marked fired when actually shown — a deferred reminder must
            # not be consumed while it waits in the hold queue.
            self.reminders.fired(p["reminder_id"])
            self._emit(REMINDER, p["reminder_id"], p["text"])
        elif event == SELF_LABEL:
            self._emit(SELF_LABEL, p["event_id"], p["app"], p["title"])
        elif event == ASK_ON_TASK:
            self._emit(ASK_ON_TASK, p["event_id"], p["intent"], p["what"])
        elif event == ASK_OUTCOME:
            self._emit(ASK_OUTCOME, p["session_id"], p["intent"])
        elif event == DRIFT:
            self._emit(DRIFT, p.get("id", 0), p["message"])
        elif event == STATUS:
            # A message-library-style line (Phase 3 uses this for the Q2 nudge;
            # Phase 5 for the message library). It self-acknowledges — there is
            # nothing to respond to — so it does not hold the one-at-a-time slot.
            self._emit(STATUS, p["message"])
            self.arbiter.acknowledge()

    def _ack_interrupt(self) -> None:
        """A response to whatever is on screen frees the one-at-a-time slot."""
        self.arbiter.acknowledge()

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

    def _load_session_hints(self) -> None:
        """Point the classifier at the current session's task hints, if any.
        Called on start and on every phase change, so hints load for a
        CLI-started task session and clear the moment the session ends."""
        from . import tasks as task_store
        session = self.sessions.current
        hints = None
        if session and session.is_open:
            tid = db.session_task_id(self.conn, session.id)
            if tid is not None:
                hints = task_store.get_hints(self.conn, tid)
        self.classifier.set_session_hints(hints)

    def _on_phase_change(self, phase, session) -> None:
        self.tracker.set_session(session.id if session else None)
        self._load_session_hints()
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
        self._flush_classifier(force=True)
        self.sessions.end(None)
        # The session ends regardless; whether the outcome prompt is shown is the
        # arbiter's call (priority 90, so it clears deep-work and quiet hours).
        self.arbiter.submit(Request(
            "session_overrun",
            payload={"event": ASK_OUTCOME, "session_id": session.id,
                     "intent": session.declared_intent},
            dedupe_key=f"overrun:{session.id}", requested_at=self.clock()))

    # --- self-label --------------------------------------------------------

    def _maybe_self_label(self) -> None:
        if not self.selflabel.due(self.sessions.phase is Phase.BREAK):
            return
        row = self.selflabel.pick_event()
        # Mark the slot used whether or not this ends up shown: the hourly gate
        # is about how often we *ask*, and re-submitting every tick would flood
        # the arbiter. The arbiter's 1/h cap is the second layer; the show/defer
        # decision is entirely its call now.
        self.selflabel.mark_asked()
        if row is None:
            return
        self.arbiter.submit(Request(
            "self_label",
            payload={"event": SELF_LABEL, "event_id": row["id"],
                     "app": row["app"] or "", "title": row["window_title"] or ""},
            dedupe_key=f"self_label:{row['id']}", requested_at=self.clock()))

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
            # Tier 4 asks are self-label questions too — same UX, same priority,
            # same 1/h cap. Submit them all; the arbiter shows at most one and
            # drops the rest over cap. Asking is honest, but not endlessly.
            self.arbiter.submit(Request(
                "self_label",
                payload={"event": ASK_ON_TASK, "event_id": pending.event_id,
                         "intent": pending.intent,
                         "what": pending.title or pending.app or "that window"},
                dedupe_key=f"ask_on_task:{pending.event_id}",
                requested_at=self.clock()))

    # --- tasks (izy-v2.md §3) ----------------------------------------------

    def _collect_hint_suggestions(self) -> None:
        """When a paid or asked verdict resolves a span on-task for a task-backed
        session, remember its app as a hint suggestion — a one-tap acceptance
        later, so Izy gets cheaper the more the task is worked. Scans only labels
        newer than the last scan (source llm/user; hint labels are 'rule' and so
        are excluded — no point suggesting what is already a hint)."""
        try:
            session = self.sessions.current
            if not session or not session.is_open:
                return
            tid = db.session_task_id(self.conn, session.id)
            if tid is None:
                return
            rows = self.conn.execute(
                "SELECT l.id, e.app FROM labels l"
                " JOIN activity_events e ON e.id = l.event_id"
                " WHERE l.id > ? AND e.session_id = ? AND l.source IN ('llm','user')"
                "   AND l.on_task = 1 AND e.app IS NOT NULL ORDER BY l.id",
                (self._last_suggest_label_id, session.id)).fetchall()
            from . import tasks as task_store
            for r in rows:
                self._last_suggest_label_id = max(self._last_suggest_label_id, r["id"])
                task_store.suggest_hint(self.conn, tid, r["app"])
        except Exception:
            log.exception("hint suggestion scan failed")

    def _maybe_q2_nudge(self) -> None:
        """Once a week, surface one Important-not-urgent task with no session in
        the last 7 days — the point of the whole matrix. Priority 10, through the
        arbiter, which will usually say no. The scan itself is throttled to once
        an hour so it costs nothing per tick."""
        try:
            now = self.clock()
            if self._last_q2_scan is not None and \
                    (now - self._last_q2_scan).total_seconds() < 3600:
                return
            self._last_q2_scan = now
            last = db.get_meta(self.conn, "last_q2_nudge")
            if last and (now - from_iso(last)).days < 7:
                return
            from . import tasks as task_store
            task = task_store.neglected_q2(self.conn, now)
            if task is None:
                return
            db.set_meta(self.conn, "last_q2_nudge", to_iso(now))
            self.arbiter.submit(Request(
                "message",
                payload={"event": STATUS,
                         "message": f"Important, not urgent, untouched this week: "
                                    f"{task.title}"},
                dedupe_key=f"q2_nudge:{task.id}", requested_at=now))
        except Exception:
            log.exception("q2 nudge failed")

    def _check_drift(self) -> None:
        try:
            session = self.sessions.current
            if not session:
                return
            message = self.drift.detect(session.id, session.declared_intent)
            if message:
                self.arbiter.submit(Request(
                    "drift",
                    payload={"event": DRIFT, "id": 0, "message": message},
                    dedupe_key=f"drift:{session.id}", requested_at=self.clock()))
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
        """Submit a due reminder to the arbiter. It is marked fired only when the
        arbiter actually shows it (see _dispatch_interrupts), so a deferred one
        is not consumed while it waits. Urgent reminders ride priority 100 and so
        clear deep-work, quiet hours and fullscreen."""
        urgent = reminder.raw_text.lower().startswith("[urgent]")
        self.arbiter.submit(Request(
            "urgent_reminder" if urgent else "reminder",
            payload={"event": REMINDER, "reminder_id": reminder.id,
                     "text": reminder.text},
            dedupe_key=f"reminder:{reminder.id}", requested_at=self.clock()))

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

    def start_session(self, intent: str, minutes: int,
                      task_id: int | None = None) -> list[Event]:
        try:
            self.sessions.start(intent, minutes, task_id=task_id)
            self._load_session_hints()
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
        self._ack_interrupt()

    def record_self_label(self, event_id: int, on_task: bool) -> None:
        self.selflabel.record(event_id, on_task)
        self._ack_interrupt()

    def skip_self_label(self) -> None:
        self.selflabel.skip()
        self._ack_interrupt()

    def record_on_task_answer(self, event_id: int, on_task: bool) -> None:
        self.classifier.record_user_answer(event_id, on_task)
        self._ack_interrupt()

    def record_drift_response(self, intervention_id: int, response: str) -> None:
        """Any response frees the one-at-a-time slot. The 90s global cooldown
        (arbiter) is what spaces the next one now, not a per-dismissal cooldown."""
        self._ack_interrupt()

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
        self._ack_interrupt()

    def reminder_snooze(self, reminder_id: int) -> None:
        self.reminders.snooze(reminder_id)
        self._ack_interrupt()

    def reminder_dismiss(self, reminder_id: int) -> None:
        self.reminders.dismiss(reminder_id)
        self._ack_interrupt()

    # --- task commands (writes go through the one writer) ------------------

    def create_task(self, **fields) -> dict:
        from . import tasks as task_store
        due = fields.pop("due_at", None)
        if isinstance(due, str) and due:
            due = from_iso(due)
        task = task_store.create(self.conn, now=self.clock(), due_at=due, **fields)
        return task.to_dict()

    def update_task(self, task_id: int, **fields) -> dict | None:
        from . import tasks as task_store
        due = fields.get("due_at")
        if isinstance(due, str) and due:
            fields["due_at"] = from_iso(due)
        task = task_store.update(self.conn, task_id, now=self.clock(), **fields)
        # If the active session's task changed its hints, reload them.
        self._load_session_hints()
        return task.to_dict() if task else None

    def set_task_quadrant(self, task_id: int, quadrant: str) -> dict | None:
        from . import tasks as task_store
        task = task_store.set_quadrant(self.conn, task_id, quadrant, now=self.clock())
        return task.to_dict() if task else None

    def reorder_task(self, task_id: int, before=None, after=None) -> dict | None:
        from . import tasks as task_store
        task = task_store.reorder(self.conn, task_id, before=before, after=after)
        return task.to_dict() if task else None

    def delete_task(self, task_id: int) -> None:
        from . import tasks as task_store
        task_store.delete(self.conn, task_id)

    def accept_hint(self, task_id: int, app=None, domain=None,
                    keyword=None) -> dict | None:
        from . import tasks as task_store
        task = task_store.add_hint(self.conn, task_id, app=app, domain=domain,
                                   keyword=keyword, now=self.clock())
        self._load_session_hints()
        return task.to_dict() if task else None
