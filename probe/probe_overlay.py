#!/usr/bin/env python3
"""Step 0 throwaway probe: can we create a frameless, translucent, always-on-top
window AND position it where we want, on this compositor?

Run once with QT_QPA_PLATFORM=wayland and once with =xcb, and compare the
requested position against the position actually granted.
NOT application code. Delete after Step 0.
"""
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

TARGET_X, TARGET_Y = 1400, 300
W, H = 48, 56


class Mascot(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput  # click-through
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(W, H)
        self.move(TARGET_X, TARGET_Y)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(90, 140, 255, 200))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(self.rect(), 10, 10)


app = QApplication(sys.argv)
m = Mascot()
m.show()
m.move(TARGET_X, TARGET_Y)  # again, after show()

plat = app.platformName()
results = {}


def check():
    g = m.geometry()
    fg = m.frameGeometry()
    results.update(
        platform=plat,
        requested=(TARGET_X, TARGET_Y),
        got=(g.x(), g.y()),
        size=(g.width(), g.height()),
        frame=(fg.x(), fg.y()),
        translucent=m.testAttribute(Qt.WA_TranslucentBackground),
        on_top=bool(m.windowFlags() & Qt.WindowStaysOnTopHint),
        clickthrough=bool(m.windowFlags() & Qt.WindowTransparentForInput),
        visible=m.isVisible(),
    )
    ok = results["got"] == results["requested"]
    print(f"platform            : {plat}")
    print(f"requested position  : {results['requested']}")
    print(f"actual position     : {results['got']}   ->  "
          f"{'POSITIONING WORKS' if ok else 'POSITION IGNORED BY COMPOSITOR'}")
    print(f"size granted        : {results['size']} (wanted {(W, H)})")
    print(f"frameless/translucent: translucent={results['translucent']}")
    print(f"always-on-top flag  : {results['on_top']}")
    print(f"click-through flag  : {results['clickthrough']}")
    print(f"window mapped       : {results['visible']}")
    app.quit()


QTimer.singleShot(1200, check)
sys.exit(app.exec())
