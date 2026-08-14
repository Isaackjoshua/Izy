#!/usr/bin/env python3
"""Verify overlay position against an INDEPENDENT observer, not Qt's own cache.

Qt on Wayland returns the geometry you asked for whether or not the compositor
honoured it, so probe_overlay.py cannot be trusted on its own. Under xcb we can
ask the X server directly (xwininfo) for the truth.
"""
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

TARGET_X, TARGET_Y = 1400, 300


class Mascot(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("IZY_PROBE_OVERLAY")
        self.resize(48, 56)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setBrush(QColor(90, 140, 255, 200))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 10, 10)


app = QApplication(sys.argv)
m = Mascot()
m.show()
m.move(TARGET_X, TARGET_Y)


def check():
    wid = int(m.winId())
    print(f"platform      : {app.platformName()}")
    print(f"qt says       : ({m.geometry().x()}, {m.geometry().y()})  [Qt's own cache]")
    if app.platformName() == "xcb":
        out = subprocess.run(["xwininfo", "-id", str(wid)],
                             capture_output=True, text=True, timeout=5).stdout
        ax = ay = None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Absolute upper-left X:"):
                ax = int(s.split(":")[1])
            elif s.startswith("Absolute upper-left Y:"):
                ay = int(s.split(":")[1])
        print(f"X SERVER says : ({ax}, {ay})  [independent ground truth]")
        print("VERDICT       : " + ("POSITIONING REALLY WORKS"
                                    if (ax, ay) == (TARGET_X, TARGET_Y)
                                    else "compositor overrode our position"))
    else:
        print("X SERVER says : n/a — native Wayland client, no external observer")
        print("VERDICT       : UNVERIFIABLE (Qt/Wayland reports the requested value "
              "regardless; wl clients cannot self-position)")
    app.quit()


QTimer.singleShot(1500, check)
sys.exit(app.exec())
