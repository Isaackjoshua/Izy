"""The mascot overlay. Phase 1 ships the static placeholder square SPEC.md asks
for — real art is Phase 5 — but the *behaviour* is built to spec now, because
that is the part that decides whether this thing survives a week of use.

Non-negotiables implemented here:
  * No idle animation. None. Motion in peripheral vision is what steals
    attention, so the only motion in this file is a state cross-fade.
  * Click-through until hovered, so it can never block what is underneath.
  * Dims to ~35% when the cursor comes near.
  * Never takes keyboard focus.

Runs under XWayland (QT_QPA_PLATFORM=xcb), set in app.py: Step 0 verified that
native Wayland clients cannot position themselves, and Mutter does not
implement layer-shell. See docs/STEP0-ENVIRONMENT.md.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QWidget

W, H = 48, 56
FADE_MS = 400  # cross-fade only; nothing else in this widget moves

# neutral (on task) / soft-alert (drifting) / asleep (no session)
STATE_COLORS = {
    "neutral": QColor(96, 140, 220),
    "soft-alert": QColor(214, 158, 92),
    "asleep": QColor(120, 124, 134),
}


class Mascot(QWidget):
    clicked = Signal()

    def __init__(self, cfg) -> None:
        super().__init__(None)
        self.cfg = cfg
        self._state = "asleep"
        self._color = QColor(STATE_COLORS["asleep"])
        self._target = QColor(STATE_COLORS["asleep"])
        self._fade_step = 0
        # None means "not applied yet", so the first call always writes the
        # attribute. Tracking it as a plain bool starting at False made the
        # opening _set_click_through(True) a no-op, and the mascot swallowed
        # clicks until the cursor happened to pass near it.
        self._click_through: bool | None = None

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool                      # no taskbar entry, no alt-tab
            | Qt.WindowDoesNotAcceptFocus  # must never steal keyboard focus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Izy")
        self.resize(W, H)
        self.setWindowOpacity(cfg.mascot.opacity)
        self._set_click_through(True)

        # Proximity dimming and hover detection. 8 Hz is under the threshold
        # where a cursor poll shows up in power use, and well above the rate a
        # hand can move a mouse into the corner unnoticed.
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(125)
        self._cursor_timer.timeout.connect(self._check_cursor)
        self._cursor_timer.start()

        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(FADE_MS // 20)
        self._fade_timer.timeout.connect(self._advance_fade)

        self._opacity_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._opacity_anim.setDuration(180)

    # --- placement ---------------------------------------------------------

    def anchor(self) -> None:
        """Park in the configured corner of the screen the cursor is on."""
        screen = self.screen() or self.windowHandle().screen()
        geo = screen.availableGeometry()
        m = self.cfg.mascot.margin_px
        corner = (self.cfg.mascot.corner or "bottom-right").lower()
        x = geo.left() + m if "left" in corner else geo.right() - W - m
        y = geo.top() + m if "top" in corner else geo.bottom() - H - m
        self.move(QPoint(int(x), int(y)))

    def showEvent(self, e):
        super().showEvent(e)
        self.anchor()

    # --- state -------------------------------------------------------------

    def set_state(self, state: str) -> None:
        if state not in STATE_COLORS or state == self._state:
            return
        self._state = state
        self._target = QColor(STATE_COLORS[state])
        self._fade_step = 0
        self._fade_timer.start()

    def _advance_fade(self) -> None:
        self._fade_step += 1
        t = min(1.0, self._fade_step / 20)
        self._color = _blend(self._color, self._target, t)
        if t >= 1.0:
            self._fade_timer.stop()
        self.update()

    # --- interaction -------------------------------------------------------

    def _set_click_through(self, on: bool) -> None:
        """Click-through by default; interactive only under the cursor."""
        if self._click_through == on:
            return
        first = self._click_through is None
        self._click_through = on
        self.setAttribute(Qt.WA_TransparentForMouseEvents, on)
        # Toggling the input region can re-map the window, which drops it back
        # to wherever the compositor feels like. Re-anchor, but not before the
        # first show(), when there is no screen to anchor to yet.
        if not first and self.isVisible():
            self.anchor()

    def _check_cursor(self) -> None:
        if not self.isVisible():
            return
        pos = QCursor.pos()
        rect = self.frameGeometry()
        dx = max(rect.left() - pos.x(), 0, pos.x() - rect.right())
        dy = max(rect.top() - pos.y(), 0, pos.y() - rect.bottom())
        dist = (dx * dx + dy * dy) ** 0.5

        near = dist <= self.cfg.mascot.proximity_px
        want = self.cfg.mascot.dim_opacity if near else self.cfg.mascot.opacity
        if abs(self.windowOpacity() - want) > 0.01:
            self._opacity_anim.stop()
            self._opacity_anim.setStartValue(self.windowOpacity())
            self._opacity_anim.setEndValue(want)
            self._opacity_anim.start()

        self._set_click_through(not rect.contains(pos))

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()

    # --- paint -------------------------------------------------------------

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 12, 12)
        # Placeholder marker so it is obvious this is not the real mascot yet.
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawRoundedRect(self.rect().adjusted(14, 18, -14, -22), 3, 3)


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )
