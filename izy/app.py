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


def run(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("IZY_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    paths.ensure_dirs()
    cfg = config.load()
    _force_xwayland()

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from .ui.mascot import Mascot
    from .ui.prompts import IntentPrompt, OutcomePrompt, SelfLabelPrompt
    from .worker import TrackerThread

    app = QApplication(argv or sys.argv)
    app.setApplicationName("Izy")
    # Closing the last popup must not exit the daemon.
    app.setQuitOnLastWindowClosed(False)

    mascot = Mascot(cfg)
    tracker = TrackerThread(cfg)
    popups: list = []   # keeps popups alive; Qt would otherwise GC them

    def keep(widget):
        popups.append(widget)
        widget.destroyed.connect(lambda: popups.remove(widget) if widget in popups else None)
        return widget

    # --- worker -> UI ------------------------------------------------------

    def on_ready(desc: str) -> None:
        log.info("tracking via %s", desc)
        if desc == "unavailable":
            log.warning("no window titles will be recorded — run: izy install-extension")

    def on_phase(phase_value: str, session) -> None:
        mascot.set_state("neutral" if phase_value == Phase.FOCUS.value else "asleep")

    def on_ask_label(event_id: int, app_name: str, title: str) -> None:
        p = keep(SelfLabelPrompt(app_name, title))
        p.answered.connect(lambda ok: tracker.worker.request_label(event_id, ok))
        p.dismissed.connect(tracker.worker.request_skip_label)
        p.place_near(mascot)
        p.show()

    tracker.worker.ready.connect(on_ready)
    tracker.worker.phase_changed.connect(on_phase)
    tracker.worker.ask_self_label.connect(on_ask_label)

    # --- UI -> worker ------------------------------------------------------

    def on_mascot_clicked() -> None:
        sm = tracker.worker.sessions
        if sm is None:
            return
        if sm.phase is Phase.FOCUS:
            current = sm.current
            p = keep(OutcomePrompt(current.declared_intent))
            p.chosen.connect(tracker.worker.request_end_session)
            p.dismissed.connect(lambda: tracker.worker.request_end_session(""))
        else:
            p = keep(IntentPrompt(cfg.session.default_minutes))
            p.submitted.connect(tracker.worker.request_start_session)
        p.place_near(mascot)
        p.show()
        p.raise_()

    mascot.clicked.connect(on_mascot_clicked)

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
