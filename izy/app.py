"""Wires the tracker thread to the mascot. This is the systemd service entry point."""
from __future__ import annotations

import logging
import os
import signal
import sys

from . import config, paths
from .sessions import Phase

log = logging.getLogger(__name__)


def _force_xwayland() -> None:
    """Run the overlay under XWayland unless told otherwise.

    Step 0 measured this: native Wayland clients cannot set their own position
    (Qt reports the requested geometry regardless, so it fails silently), and
    Mutter does not implement layer-shell. XWayland positions correctly, which
    the mascot's corner anchoring depends on. Set IZY_QPA to override.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    override = os.environ.get("IZY_QPA")
    if override:
        os.environ["QT_QPA_PLATFORM"] = override
    elif os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def _build_bridge():
    """Define the UI bridge lazily, so importing this module needs no Qt."""
    from PySide6.QtCore import QObject, Slot

    class UiBridge(QObject):
        """Receives every worker signal, on the GUI thread.

        This class exists for one reason, and it is not organisation. Qt decides
        which thread a slot runs on from the *receiver's* thread affinity, and a
        plain Python function has none — so connecting the worker's signals to
        bare functions ran them directly on the worker thread, which built
        QWidgets and started QTimers off the GUI thread and segfaulted the
        daemon. A QObject constructed on the main thread has main-thread
        affinity, so Qt queues these calls there automatically.
        """

        def __init__(self, cfg, mascot, worker) -> None:
            super().__init__()
            self.cfg = cfg
            self.mascot = mascot
            self.worker = worker
            self._popups: list = []   # Qt would otherwise garbage-collect these

        def _keep(self, widget):
            self._popups.append(widget)
            widget.destroyed.connect(lambda: self._forget(widget))
            return widget

        def _forget(self, widget) -> None:
            if widget in self._popups:
                self._popups.remove(widget)

        def _place(self, widget, *, focus: bool = False):
            """Position beside the mascot, stacked clear of anything already open.

            place_near() derives its position from the mascot alone, so two
            reminders coming due together landed on exactly the same pixels and
            the lower one was invisible. Offset by what is already on screen,
            growing away from the mascot's corner.
            """
            widget.place_near(self.mascot)
            open_popups = [w for w in self._popups
                           if w is not widget and w.isVisible()]
            if open_popups:
                screen = self.mascot.screen() or widget.screen()
                bottom_anchored = (self.mascot.frameGeometry().center().y()
                                   > screen.availableGeometry().center().y())
                offset = sum(w.height() + 8 for w in open_popups)
                pos = widget.pos()
                widget.move(pos.x(),
                            pos.y() - offset if bottom_anchored else pos.y() + offset)
            widget.show()
            if focus:
                widget.raise_()
            return widget

        # --- worker -> UI --------------------------------------------------

        @Slot(str)
        def on_ready(self, desc: str) -> None:
            log.info("tracking via %s", desc)
            if desc == "unavailable":
                log.warning("no window titles will be recorded — "
                            "run: izy install-extension")

        @Slot(str, object)
        def on_phase(self, phase_value: str, session) -> None:
            """Phase changes no longer drive the mascot — the pipeline derives
            its state every tick and sends it, so drifting has a way back."""

        @Slot(str)
        def on_mascot_state(self, state: str) -> None:
            self.mascot.set_state(state)

        @Slot(int, str)
        def on_ask_outcome(self, session_id: int, intent: str) -> None:
            """The planned time is up — SPEC.md Feature 1 asks how it went."""
            from .ui.prompts import OutcomePrompt
            p = self._keep(OutcomePrompt(intent))
            p.chosen.connect(
                lambda outcome: self.worker.request_outcome(session_id, outcome))
            self._place(p)

        @Slot(int, str, str)
        def on_ask_label(self, event_id: int, app_name: str, title: str) -> None:
            from .ui.prompts import SelfLabelPrompt
            p = self._keep(SelfLabelPrompt(app_name, title))
            p.answered.connect(lambda ok: self.worker.request_label(event_id, ok))
            p.dismissed.connect(self.worker.request_skip_label)
            self._place(p)

        @Slot(int, str)
        def on_show_reminder(self, reminder_id: int, text: str) -> None:
            from .ui.prompts import ReminderBubble
            b = self._keep(ReminderBubble(text, self.cfg.reminders.snooze_minutes))
            b.done.connect(lambda: self.worker.request_reminder_done(reminder_id))
            b.snoozed.connect(lambda: self.worker.request_reminder_snooze(reminder_id))
            b.dismissed_reminder.connect(
                lambda: self.worker.request_reminder_dismiss(reminder_id))
            # Escape with no choice made is a dismissal, not a silent drop.
            b.dismissed.connect(
                lambda: self.worker.request_reminder_dismiss(reminder_id))
            self._place(b)

        @Slot(str)
        def on_confirm_reminder(self, original: str) -> None:
            """Nothing readable in it, so ask instead of inventing a time."""
            from .ui.prompts import ConfirmReminderPrompt
            c = self._keep(ConfirmReminderPrompt(original))
            c.submitted.connect(
                lambda when: self.worker.request_add_reminder(f"{original} {when}"))
            self._place(c, focus=True)

        @Slot(int, str, str)
        def on_ask_on_task(self, event_id: int, intent: str, what: str) -> None:
            from .ui.prompts import OnTaskPrompt
            p = self._keep(OnTaskPrompt(intent, what))
            p.answered.connect(
                lambda ok: self.worker.request_on_task_answer(event_id, ok))
            self._place(p)

        @Slot(int, str)
        def on_show_drift(self, intervention_id: int, message: str) -> None:
            from .ui.prompts import DriftAlert
            a = self._keep(DriftAlert(message))
            a.acknowledged.connect(
                lambda: self.worker.request_drift_response(intervention_id,
                                                           "acknowledged"))
            a.dismissed_drift.connect(
                lambda: self.worker.request_drift_response(intervention_id,
                                                           "dismissed"))
            # Ignoring an alert is a dismissal: it starts the cooldown, which is
            # the behaviour that keeps Izy from becoming something you close.
            a.dismissed.connect(
                lambda: self.worker.request_drift_response(intervention_id,
                                                           "dismissed"))
            self._place(a)

        @Slot(str)
        def on_status(self, msg: str) -> None:
            log.info("%s", msg)

        # --- UI -> worker --------------------------------------------------

        @Slot()
        def on_mascot_clicked(self) -> None:
            from .ui.prompts import IntentPrompt, OutcomePrompt
            sessions = self.worker.sessions
            if sessions is None:
                return
            if sessions.phase is Phase.FOCUS:
                p = self._keep(OutcomePrompt(sessions.current.declared_intent))
                p.chosen.connect(self.worker.request_end_session)
                p.dismissed.connect(lambda: self.worker.request_end_session(""))
            else:
                p = self._keep(IntentPrompt(self.cfg.session.default_minutes))
                p.submitted.connect(self.worker.request_start_session)
                p.reminder.connect(self.worker.request_add_reminder)
            self._place(p, focus=True)

    return UiBridge


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("IZY_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    paths.ensure_dirs()
    cfg = config.load()
    _force_xwayland()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.mascot import Mascot
    from .worker import TrackerThread

    app = QApplication(argv or sys.argv)
    app.setApplicationName("Izy")
    # Closing the last popup must not exit the daemon.
    app.setQuitOnLastWindowClosed(False)

    mascot = Mascot(cfg)
    tracker = TrackerThread(cfg)
    bridge = _build_bridge()(cfg, mascot, tracker.worker)

    tracker.worker.ready.connect(bridge.on_ready)
    tracker.worker.phase_changed.connect(bridge.on_phase)
    tracker.worker.ask_self_label.connect(bridge.on_ask_label)
    tracker.worker.show_reminder.connect(bridge.on_show_reminder)
    tracker.worker.ask_on_task.connect(bridge.on_ask_on_task)
    tracker.worker.show_drift.connect(bridge.on_show_drift)
    tracker.worker.mascot_state.connect(bridge.on_mascot_state)
    tracker.worker.ask_outcome.connect(bridge.on_ask_outcome)
    tracker.worker.confirm_reminder.connect(bridge.on_confirm_reminder)
    tracker.worker.status.connect(bridge.on_status)
    mascot.clicked.connect(bridge.on_mascot_clicked)

    # --- lifecycle ---------------------------------------------------------

    def shutdown(*_):
        log.info("shutting down")
        tracker.stop()
        app.quit()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    # Let the Python signal handlers run between Qt events.
    wake = QTimer()
    wake.setInterval(500)
    wake.timeout.connect(lambda: None)
    wake.start()

    app.aboutToQuit.connect(tracker.stop)

    tracker.start()
    mascot.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
