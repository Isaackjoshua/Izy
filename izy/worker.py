"""The tracking thread.

Threading model (decided up front, per SPEC.md's working agreement): one
process, Qt on the main thread, one worker thread here doing watcher polls and
SQLite writes, talking to the UI only through Qt signals. A hung D-Bus call or
a slow disk therefore cannot stutter or freeze the mascot.

The SQLite connection is created *on this thread* and never touched from the
UI thread. The UI asks for things by emitting into `request_*` slots, which Qt
queues onto this thread's event loop.
"""
from __future__ import annotations

import logging

from dataclasses import replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from . import db
from .budget import InterruptionBudget
from .llm import LLM
from .reminders import ReminderScheduler, parse as parse_reminder
from .reminders import store as reminder_store
from .selflabel import SelfLabelPrompt
from .sessions import Phase, SessionManager
from .tracker import Tracker
from .watchers import pick_watcher

log = logging.getLogger(__name__)


class TrackerWorker(QObject):
    """Lives on the worker thread. Owns the DB connection and the watcher."""

    # worker -> UI
    ready = Signal(str)                 # watcher description
    phase_changed = Signal(str, object)  # phase value, Session|None
    ask_self_label = Signal(int, str, str)  # event_id, app, title
    show_reminder = Signal(int, str)        # reminder_id, text
    confirm_reminder = Signal(str)          # original text we could not parse
    status = Signal(str)

    def __init__(self, cfg, db_path=None) -> None:
        super().__init__()
        self.cfg = cfg
        self._db_path = db_path
        self.conn = None
        self.watcher = None
        self.tracker = None
        self.sessions = None
        self.selflabel = None
        self.budget = None
        self.llm = None
        self.reminders = None
        self._timer = None
        self._last_resync = None
        self._last_eod_check = None
        self._last_phase = None
        self._seen_apps: set[str] = set()

    @Slot()
    def start(self) -> None:
        self.conn = db.connect(self._db_path)
        self.watcher = pick_watcher(self.cfg.watcher)
        self.tracker = Tracker(
            self.conn,
            flush_interval_s=self.cfg.watcher.flush_interval_s,
            stale_after_s=max(60.0, self.cfg.watcher.poll_interval_s * 30),
        )
        self.sessions = SessionManager(self.conn, self.cfg)
        self.selflabel = SelfLabelPrompt(self.conn, self.cfg)
        self.budget = InterruptionBudget(self.conn, self.cfg)
        self.llm = LLM(self.conn, self.cfg)
        self.reminders = ReminderScheduler(self.conn, self.cfg)

        self.sessions.on_change(self._on_phase_change)
        self.sessions.recover()

        describe = getattr(self.watcher, "describe", None)
        self.ready.emit(describe() if describe else self.watcher.name)

        self._timer = QTimer()
        self._timer.setInterval(int(self.cfg.watcher.poll_interval_s * 1000))
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._on_phase_change(self.sessions.phase, self.sessions.current)

    @Slot()
    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
        if self.tracker:
            self.tracker.stop()
        if self.watcher:
            self.watcher.close()
        if self.conn:
            self.conn.close()

    # --- per-tick ----------------------------------------------------------

    #: How often to re-read session state written by `izy start` / `izy stop`
    #: in another process. Cheap (one indexed row) and bounds how much activity
    #: a CLI-started session can lose to a NULL session_id.
    RESYNC_EVERY_S = 3.0

    def _tick(self) -> None:
        self._maybe_resync()
        try:
            snap = self.watcher.poll()
        except Exception:
            log.exception("watcher poll failed")
            snap = None
        try:
            self.tracker.tick(snap)
        except Exception:
            log.exception("tracker tick failed")

        if self.sessions.is_overrun():
            self._maybe_overrun()
        self._maybe_self_label()
        self._check_reminders(snap)

    # --- reminders ---------------------------------------------------------

    def _check_reminders(self, snap) -> None:
        """Time reminders, app_opened triggers, and end_of_day. Phase changes
        are handled in _on_phase_change, which is the only place that knows a
        boundary was actually crossed."""
        try:
            phase = self.sessions.phase
            for r in self.reminders.due_now(phase):
                self._fire(r)

            if snap is not None and snap.app:
                app = snap.app.lower()
                if app not in self._seen_apps:
                    self._seen_apps.add(app)
                    for r in self.reminders.on_context(f"app_opened:{app}"):
                        self._fire(r)

            if self.reminders.end_of_day_due(self._last_eod_check):
                self._last_eod_check = self.sessions.clock()
                for r in self.reminders.on_context("end_of_day"):
                    self._fire(r)
        except Exception:
            log.exception("reminder check failed")

    def _fire(self, reminder) -> None:
        self.reminders.fired(reminder.id)
        self.show_reminder.emit(reminder.id, reminder.text)

    def _maybe_resync(self) -> None:
        now = self.sessions.clock()
        if self._last_resync is not None and \
                (now - self._last_resync).total_seconds() < self.RESYNC_EVERY_S:
            return
        self._last_resync = now
        try:
            self.sessions.resync()
        except Exception:
            log.exception("session resync failed")

    def _maybe_self_label(self) -> None:
        if not self.selflabel.due(self.sessions.phase is Phase.BREAK):
            return
        allowed, reason = self.budget.check("self_label")
        if not allowed:
            log.debug("self-label suppressed: %s", reason)
            # Charge the slot anyway so a suppressed prompt does not queue up
            # and fire in a burst the moment the budget frees.
            self.selflabel.last_asked = self.selflabel.clock()
            return
        row = self.selflabel.pick_event()
        if row is None:
            self.selflabel.last_asked = self.selflabel.clock()
            return
        self.ask_self_label.emit(row["id"], row["app"] or "", row["window_title"] or "")

    def _maybe_overrun(self) -> None:
        allowed, reason = self.budget.check("session_overrun")
        if not allowed:
            log.debug("overrun notice suppressed: %s", reason)
            return
        # Phase 1 closes it quietly rather than nagging; the outcome prompt is
        # asked the next time the mascot is clicked.
        self.sessions.end(None)

    # --- slots the UI calls ------------------------------------------------

    @Slot(str, int)
    def request_start_session(self, intent: str, minutes: int) -> None:
        try:
            self.sessions.start(intent, minutes)
        except ValueError as e:
            self.status.emit(str(e))

    @Slot(str)
    def request_end_session(self, outcome: str) -> None:
        self.sessions.end(outcome or None)

    @Slot(int, bool)
    def request_label(self, event_id: int, on_task: bool) -> None:
        self.selflabel.record(event_id, on_task)

    @Slot()
    def request_skip_label(self) -> None:
        self.selflabel.skip()

    @Slot(str)
    def request_add_reminder(self, raw: str) -> None:
        """Parse and store. Asks rather than guessing when nothing is readable —
        SPEC.md forbids inventing a time that was not stated."""
        try:
            parsed = parse_reminder(raw, self.llm)
        except Exception:
            log.exception("reminder parse failed")
            self.confirm_reminder.emit(raw)
            return
        if not parsed.is_valid():
            self.confirm_reminder.emit(raw)
            return
        if parsed.urgent:
            parsed = replace(parsed, text=f"[urgent] {parsed.text}")
        r = reminder_store.add(self.conn, parsed)
        when = r.due_at.astimezone().strftime("%H:%M") if r.due_at else r.trigger_context
        self.status.emit(f"reminder saved for {when}")

    @Slot(int)
    def request_reminder_done(self, reminder_id: int) -> None:
        self.reminders.done(reminder_id)

    @Slot(int)
    def request_reminder_snooze(self, reminder_id: int) -> None:
        self.reminders.snooze(reminder_id)

    @Slot(int)
    def request_reminder_dismiss(self, reminder_id: int) -> None:
        self.reminders.dismiss(reminder_id)

    def _on_phase_change(self, phase, session) -> None:
        self.tracker.set_session(session.id if session else None)
        old, self._last_phase = self._last_phase, phase
        if self.reminders is not None and old is not None:
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
        self.phase_changed.emit(phase.value, session)


class TrackerThread:
    """Owns the QThread and keeps the worker alive on it."""

    def __init__(self, cfg, db_path=None) -> None:
        self.thread = QThread()
        self.thread.setObjectName("izy-tracker")
        self.worker = TrackerWorker(cfg, db_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.start)
        self._stopped = False

    def start(self) -> None:
        self._stopped = False
        self.thread.start()

    def stop(self, timeout_ms: int = 5000) -> None:
        """Idempotent, and safe to call once the thread is already gone.

        Both the SIGTERM handler and aboutToQuit call this, so a second call is
        the normal path, not an edge case — and a BlockingQueuedConnection to a
        thread that has already quit deadlocks forever, which on a systemd
        service means every logout ends in a SIGKILL.
        """
        if self._stopped:
            return
        self._stopped = True
        from PySide6.QtCore import QMetaObject, Qt
        if self.thread.isRunning():
            QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
            self.thread.quit()
            if not self.thread.wait(timeout_ms):
                log.warning("tracker thread did not exit in %dms; terminating", timeout_ms)
                self.thread.terminate()
                self.thread.wait(1000)
        else:
            self.worker.stop()
