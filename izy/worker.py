"""The tracking thread — Qt plumbing only.

Threading model (decided up front, per SPEC.md's working agreement): one
process, Qt on the main thread, one worker thread here doing watcher polls and
SQLite writes, talking to the UI only through Qt signals. A hung D-Bus call or
a slow disk therefore cannot stutter or freeze the mascot.

All the actual policy lives in `pipeline.py`, which has no Qt in it. This file
does three things: run the pipeline's clock on a QTimer, turn the events it
returns into signals, and forward the UI's requests back into it. Keeping it
that thin is what makes the daemon's behaviour testable.

The SQLite connection is created *on this thread* and never touched from the
UI thread. The UI asks for things by emitting into `request_*` slots, which Qt
queues onto this thread's event loop.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from . import pipeline as pl

log = logging.getLogger(__name__)


class TrackerWorker(QObject):
    """Lives on the worker thread. Owns the pipeline, and therefore the DB."""

    # worker -> UI
    ready = Signal(str)                     # watcher description
    phase_changed = Signal(str, object)     # phase value, Session|None
    ask_self_label = Signal(int, str, str)  # event_id, app, title
    show_reminder = Signal(int, str)        # reminder_id, text
    ask_on_task = Signal(int, str, str)     # tier 4: event_id, intent, what
    show_drift = Signal(int, str)           # intervention_id, message
    confirm_reminder = Signal(str)          # text we could not parse
    mascot_state = Signal(str)              # neutral | soft-alert | asleep
    ask_outcome = Signal(int, str)          # session_id, intent
    status = Signal(str)

    def __init__(self, cfg, db_path=None) -> None:
        super().__init__()
        self.cfg = cfg
        self._db_path = db_path
        self.pipeline: pl.Pipeline | None = None
        self._timer = None

    @property
    def sessions(self):
        """The mascot asks the current phase when it is clicked."""
        return self.pipeline.sessions if self.pipeline else None

    @Slot()
    def start(self) -> None:
        self.pipeline = pl.Pipeline(self.cfg, self._db_path)
        self.ready.emit(self.pipeline.describe())
        self._dispatch(self.pipeline.start())

        self._timer = QTimer()
        self._timer.setInterval(int(self.cfg.watcher.poll_interval_s * 1000))
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @Slot()
    def stop(self) -> None:
        if self._timer:
            self._timer.stop()
        if self.pipeline:
            self.pipeline.stop()
            self.pipeline = None

    def _tick(self) -> None:
        self._dispatch(self.pipeline.tick())

    # --- events -> signals -------------------------------------------------

    def _dispatch(self, events) -> None:
        for kind, payload in events:
            signal = self._SIGNALS.get(kind)
            if signal is None:
                log.warning("unhandled pipeline event: %s", kind)
                continue
            getattr(self, signal).emit(*payload)

    _SIGNALS = {
        pl.PHASE: "phase_changed",
        pl.SELF_LABEL: "ask_self_label",
        pl.REMINDER: "show_reminder",
        pl.CONFIRM_REMINDER: "confirm_reminder",
        pl.ASK_ON_TASK: "ask_on_task",
        pl.DRIFT: "show_drift",
        pl.MASCOT: "mascot_state",
        pl.ASK_OUTCOME: "ask_outcome",
        pl.STATUS: "status",
    }

    # --- slots the UI calls ------------------------------------------------

    @Slot(str, int)
    def request_start_session(self, intent: str, minutes: int) -> None:
        self._dispatch(self.pipeline.start_session(intent, minutes))

    @Slot(str)
    def request_end_session(self, outcome: str) -> None:
        self._dispatch(self.pipeline.end_session(outcome or None))

    @Slot(int, str)
    def request_outcome(self, session_id: int, outcome: str) -> None:
        self.pipeline.record_outcome(session_id, outcome)

    @Slot(int, bool)
    def request_label(self, event_id: int, on_task: bool) -> None:
        self.pipeline.record_self_label(event_id, on_task)

    @Slot()
    def request_skip_label(self) -> None:
        self.pipeline.skip_self_label()

    @Slot(int, bool)
    def request_on_task_answer(self, event_id: int, on_task: bool) -> None:
        self.pipeline.record_on_task_answer(event_id, on_task)

    @Slot(int, str)
    def request_drift_response(self, intervention_id: int, response: str) -> None:
        self.pipeline.record_drift_response(intervention_id, response)

    @Slot(str)
    def request_add_reminder(self, raw: str) -> None:
        self._dispatch(self.pipeline.add_reminder(raw))

    @Slot(int)
    def request_reminder_done(self, reminder_id: int) -> None:
        self.pipeline.reminder_done(reminder_id)

    @Slot(int)
    def request_reminder_snooze(self, reminder_id: int) -> None:
        self.pipeline.reminder_snooze(reminder_id)

    @Slot(int)
    def request_reminder_dismiss(self, reminder_id: int) -> None:
        self.pipeline.reminder_dismiss(reminder_id)


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
                log.warning("tracker thread did not exit in %dms; terminating",
                            timeout_ms)
                self.thread.terminate()
                self.thread.wait(1000)
        else:
            self.worker.stop()
