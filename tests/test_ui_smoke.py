"""UI smoke tests: the widgets construct, carry the behaviour SPEC.md requires,
and emit what app.py listens for.

Skipped when there is no display, so the suite still passes headless.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    pytest.skip("no display available", allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from izy.config import Config  # noqa: E402
from izy.ui.mascot import STATE_COLORS, Mascot  # noqa: E402
from izy.ui.prompts import IntentPrompt, OutcomePrompt, SelfLabelPrompt  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_mascot_never_steals_focus_and_starts_click_through(qapp):
    m = Mascot(Config())
    flags = m.windowFlags()
    assert flags & Qt.WindowDoesNotAcceptFocus, "must never take keyboard focus"
    assert flags & Qt.FramelessWindowHint
    assert flags & Qt.WindowStaysOnTopHint
    assert m.testAttribute(Qt.WA_TransparentForMouseEvents), "click-through by default"
    assert m.testAttribute(Qt.WA_TranslucentBackground)
    assert (m.width(), m.height()) == (48, 56)
    m.close()


def test_mascot_has_exactly_the_three_specified_states(qapp):
    assert set(STATE_COLORS) == {"neutral", "soft-alert", "asleep"}
    m = Mascot(Config())
    assert m._state == "asleep"
    m.set_state("neutral")
    assert m._state == "neutral"
    m.set_state("nonsense")
    assert m._state == "neutral", "unknown states are ignored, not applied"
    m.close()


def test_intent_prompt_emits_text_and_duration(qapp):
    p = IntentPrompt(25)
    got = []
    p.submitted.connect(lambda t, n: got.append((t, n)))
    p.edit.setText("fix the dataloader")
    p.spin.setValue(40)
    p._submit()
    assert got == [("fix the dataloader", 40)]


def test_intent_prompt_refuses_an_empty_intent(qapp):
    p = IntentPrompt(25)
    got = []
    p.submitted.connect(lambda t, n: got.append(t))
    p.edit.setText("   ")
    p._submit()
    assert got == [], "a session with no declared intent is meaningless"


def test_outcome_prompt_offers_the_three_spec_outcomes(qapp):
    from PySide6.QtWidgets import QPushButton
    p = OutcomePrompt("fix the dataloader")
    labels = [b.text() for b in p.findChildren(QPushButton)]
    assert labels == ["Finished", "Partly", "No"]
    # Qt.Tool is itself (Dialog | Popup), so a flag test cannot show modality.
    assert p.isModal() is False, "never modal"
    assert p.windowFlags() & Qt.WindowDoesNotAcceptFocus, "never steals focus"
    p.close()


def test_self_label_prompt_emits_on_and_off_task(qapp):
    for want in (True, False):
        p = SelfLabelPrompt("Code", "dataloader.py")
        got = []
        p.answered.connect(got.append)
        from PySide6.QtWidgets import QPushButton
        btn = next(b for b in p.findChildren(QPushButton)
                   if b.text() == ("On task" if want else "Off task"))
        btn.click()
        assert got == [want]


def test_prompt_text_follows_the_tone_rules(qapp):
    """No exclamation marks, no emoji, no motivational filler — SPEC.md."""
    from PySide6.QtWidgets import QLabel, QPushButton
    for widget in (OutcomePrompt("fix the dataloader"),
                   SelfLabelPrompt("Code", "dataloader.py"),
                   IntentPrompt(25)):
        texts = [w.text() for w in widget.findChildren(QLabel)]
        texts += [w.text() for w in widget.findChildren(QPushButton)]
        blob = " ".join(texts)
        assert "!" not in blob
        assert all(ord(c) < 0x2100 or c == "—" or c == "…" for c in blob), blob


# --- the UI bridge ---------------------------------------------------------

class _FakeWorker:
    """Records what the bridge asks the worker to do."""
    sessions = None

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
        return record


def _bridge(qapp, mascot):
    from izy.app import _build_bridge
    return _build_bridge()(Config(), mascot, _FakeWorker())


def test_every_worker_signal_lands_on_a_main_thread_qobject(qapp):
    """The bridge exists because plain-function slots ran on the worker thread
    and segfaulted the daemon by building widgets off the GUI thread. Qt picks
    the thread from the receiver's affinity, so every handler must be a slot on
    a QObject, not a bare function."""
    from PySide6.QtCore import QObject
    from izy.app import _build_bridge

    UiBridge = _build_bridge()
    assert issubclass(UiBridge, QObject)
    for name in ("on_ready", "on_phase", "on_ask_label", "on_show_reminder",
                 "on_confirm_reminder", "on_status", "on_mascot_clicked"):
        assert callable(getattr(UiBridge, name)), f"{name} missing from the bridge"


def test_simultaneous_reminders_do_not_land_on_top_of_each_other(qapp):
    """place_near derives position from the mascot alone, so without stacking
    two reminders due at once occupy identical pixels and one is invisible."""
    m = Mascot(Config())
    m.show()
    b = _bridge(qapp, m)
    b.on_show_reminder(1, "refill water")
    b.on_show_reminder(2, "push the branch")

    positions = [w.pos().y() for w in b._popups]
    assert len(positions) == 2
    assert len(set(positions)) == 2, "reminders must not overlap"
    m.close()


def test_reminder_buttons_reach_the_worker(qapp):
    from PySide6.QtWidgets import QPushButton
    m = Mascot(Config())
    b = _bridge(qapp, m)
    b.on_show_reminder(7, "take the pizza out")
    bubble = b._popups[-1]

    next(x for x in bubble.findChildren(QPushButton) if x.text() == "Done").click()
    assert ("request_reminder_done", (7,)) in b.worker.calls
    m.close()


def test_reminder_bubble_never_steals_focus_and_is_not_modal(qapp):
    from izy.ui.prompts import ReminderBubble
    r = ReminderBubble("refill water", 10)
    assert r.windowFlags() & Qt.WindowDoesNotAcceptFocus
    assert r.isModal() is False
    r.close()


def test_remind_me_in_the_intent_box_is_routed_to_reminders(qapp):
    """SPEC.md Feature 3 puts reminders in the same box as the session intent."""
    from izy.ui.prompts import IntentPrompt
    p = IntentPrompt(25)
    intents, reminders = [], []
    p.submitted.connect(lambda t, n: intents.append(t))
    p.reminder.connect(reminders.append)

    p.edit.setText("remind me to email the supervisor at 4pm")
    p._submit()
    assert reminders == ["remind me to email the supervisor at 4pm"]
    assert intents == [], "a reminder must not start a focus session"

    p2 = IntentPrompt(25)
    got = []
    p2.submitted.connect(lambda t, n: got.append(t))
    p2.edit.setText("fix the dataloader")
    p2._submit()
    assert got == ["fix the dataloader"]
