"""Small frameless popups: the intent box, the outcome question, and the
hourly self-label bubble.

Every one of these is a `Qt.Tool` popup that appears near the mascot. None is
modal, none steals keyboard focus from whatever you were typing in — except the
intent box, which you opened deliberately by clicking, and which therefore does
want the caret. Dismissal is always available with Escape.

Tone rules from SPEC.md Feature 4 apply to every string in this file: no guilt
language, no exclamation marks, no emoji, no motivational filler.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)

_STYLE = """
QFrame#card {
    background: rgba(30, 32, 38, 242);
    border: 1px solid rgba(255,255,255,26);
    border-radius: 10px;
}
QLabel { color: #e8e9ec; }
QLabel#dim { color: #a2a6b0; }
QLineEdit {
    background: rgba(255,255,255,16); border: 1px solid rgba(255,255,255,34);
    border-radius: 6px; padding: 6px 8px; color: #f2f3f5;
}
QSpinBox {
    background: rgba(255,255,255,16); border: 1px solid rgba(255,255,255,34);
    border-radius: 6px; padding: 4px 6px; color: #f2f3f5;
}
QPushButton {
    background: rgba(255,255,255,20); border: 1px solid rgba(255,255,255,30);
    border-radius: 6px; padding: 5px 12px; color: #e8e9ec;
}
QPushButton:hover { background: rgba(255,255,255,34); }
QPushButton#primary { background: rgba(96,140,220,190); border-color: rgba(96,140,220,220); }
"""


class Popup(QWidget):
    """Base: frameless card, Escape closes, positions itself beside the mascot."""

    dismissed = Signal()

    def __init__(self, *, focusable: bool) -> None:
        super().__init__(None)
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        if not focusable:
            flags |= Qt.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        if not focusable:
            self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame(self)
        self.card.setObjectName("card")
        outer.addWidget(self.card)
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(14, 12, 14, 12)
        self.body.setSpacing(8)

    def place_near(self, anchor_widget) -> None:
        """Sit just inside the mascot's corner, never off-screen."""
        self.adjustSize()
        screen = anchor_widget.screen() or self.screen()
        geo = screen.availableGeometry()
        a = anchor_widget.frameGeometry()
        x = a.right() - self.width() if a.center().x() > geo.center().x() else a.left()
        y = a.top() - self.height() - 10 if a.center().y() > geo.center().y() \
            else a.bottom() + 10
        x = max(geo.left() + 8, min(x, geo.right() - self.width() - 8))
        y = max(geo.top() + 8, min(y, geo.bottom() - self.height() - 8))
        self.move(int(x), int(y))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.dismissed.emit()
            self.close()
        else:
            super().keyPressEvent(e)


class IntentPrompt(Popup):
    """'What are you working on?' plus a duration. Opened by clicking the
    mascot, so it is allowed to take focus — you asked for the caret."""

    submitted = Signal(str, int)

    def __init__(self, default_minutes: int) -> None:
        super().__init__(focusable=True)
        label = QLabel("What are you working on?")
        f = QFont(); f.setPointSize(11); label.setFont(f)
        self.body.addWidget(label)

        self.edit = QLineEdit()
        self.edit.setMinimumWidth(320)
        self.edit.setPlaceholderText("fix the dataloader")
        self.body.addWidget(self.edit)

        row = QHBoxLayout()
        row.setSpacing(6)
        mins = QLabel("for"); mins.setObjectName("dim")
        row.addWidget(mins)
        self.spin = QSpinBox()
        self.spin.setRange(1, 240)
        self.spin.setValue(default_minutes)
        self.spin.setSuffix(" min")
        row.addWidget(self.spin)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        start = QPushButton("Start"); start.setObjectName("primary"); start.setDefault(True)
        row.addWidget(cancel); row.addWidget(start)
        self.body.addLayout(row)

        start.clicked.connect(self._submit)
        self.edit.returnPressed.connect(self._submit)
        cancel.clicked.connect(lambda: (self.dismissed.emit(), self.close()))

    def _submit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            self.edit.setPlaceholderText("say what you are working on")
            return
        self.submitted.emit(text, self.spin.value())
        self.close()

    def showEvent(self, e):
        super().showEvent(e)
        self.edit.setFocus()


class OutcomePrompt(Popup):
    """Asked at session end. Three buttons, no free text — it has to be
    answerable in under a second or it will not get answered at all."""

    chosen = Signal(str)

    def __init__(self, intent: str) -> None:
        super().__init__(focusable=False)
        self.body.addWidget(QLabel(_ellipsize(f"You said: {intent}")))
        q = QLabel("Did you finish it?"); q.setObjectName("dim")
        self.body.addWidget(q)

        row = QHBoxLayout(); row.setSpacing(6)
        for label, value in (("Finished", "finished"), ("Partly", "partly"), ("No", "no")):
            b = QPushButton(label)
            if value == "finished":
                b.setObjectName("primary")
            b.clicked.connect(lambda _=False, v=value: (self.chosen.emit(v), self.close()))
            row.addWidget(b)
        self.body.addLayout(row)


class SelfLabelPrompt(Popup):
    """The hourly 'were you on task?' question, about one real logged event."""

    answered = Signal(bool)

    def __init__(self, app: str, title: str) -> None:
        super().__init__(focusable=False)
        self.body.addWidget(QLabel(_ellipsize(title or app or "that window")))
        sub = QLabel("Was this on task?"); sub.setObjectName("dim")
        self.body.addWidget(sub)

        row = QHBoxLayout(); row.setSpacing(6)
        on = QPushButton("On task"); on.setObjectName("primary")
        off = QPushButton("Off task")
        later = QPushButton("Skip")
        on.clicked.connect(lambda: (self.answered.emit(True), self.close()))
        off.clicked.connect(lambda: (self.answered.emit(False), self.close()))
        later.clicked.connect(lambda: (self.dismissed.emit(), self.close()))
        row.addWidget(on); row.addWidget(off); row.addStretch(1); row.addWidget(later)
        self.body.addLayout(row)


def _ellipsize(s: str, limit: int = 64) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"
