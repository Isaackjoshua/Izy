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

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from . import db
from .budget import InterruptionBudget
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
        self._timer = None
        self._last_resync = None

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

    def _on_phase_change(self, phase, session) -> None:
        self.tracker.set_session(session.id if session else None)
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
