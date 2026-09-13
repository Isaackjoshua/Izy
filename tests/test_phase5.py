"""Phase 5: mascot art and states, Pomodoro polish, the capture tier.

The capture tests are the important ones here. That feature can only ever fail
in one direction that matters, and these check it fails the other way.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from izy import db, pipeline as pl
from izy.capture import DEFAULT_BLOCKLIST, CaptureGate, backend_for, describe
from izy.models import Snapshot
from izy.sessions import Phase
from izy.ui import art

from .test_pipeline import FakeWatcher, _kinds, _payload, _pipeline


# --- the art ---------------------------------------------------------------

@pytest.mark.parametrize("state", art.STATES)
def test_every_state_renders_valid_standalone_svg(state):
    import xml.etree.ElementTree as ET
    svg = art.svg_for(state)
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")
    assert root.get("viewBox") == "0 0 48 56", "one SVG unit is one logical pixel"


def test_the_three_states_are_visually_distinct():
    rendered = {s: art.svg_for(s) for s in art.STATES}
    assert len(set(rendered.values())) == 3


def test_posture_carries_state_not_only_colour():
    """Someone who cannot distinguish the colours must still read the state."""
    neutral = art.svg_for("neutral")
    alert = art.svg_for("soft-alert")
    asleep = art.svg_for("asleep")

    assert "rotate" in alert and "rotate" not in neutral, "drifting leans"
    # Sleeping uses closed-eye strokes rather than filled pupils.
    assert "<path" in asleep.split("</path>")[-1] or "stroke-linecap" in asleep
    assert asleep.count("<circle") == 0, "closed eyes are strokes, not circles"


def test_pupils_stay_inside_their_eyes():
    """A stronger glance once pushed the pupils clean outside the whites."""
    import re
    for state in art.STATES:
        circles = re.findall(r"<circle cx='([\d.\-]+)' cy='([\d.\-]+)' r='([\d.]+)'",
                             art.svg_for(state))
        whites = [c for c in circles if float(c[2]) > art.PUPIL_R]
        pupils = [c for c in circles if float(c[2]) == art.PUPIL_R]
        for pupil in pupils:
            nearest = min(whites, key=lambda w: abs(float(w[0]) - float(pupil[0])))
            offset = abs(float(nearest[0]) - float(pupil[0]))
            assert offset <= float(nearest[2]) - art.PUPIL_R + 0.01, \
                f"{state}: pupil escapes its eye by {offset:.2f}"


def test_unknown_states_fall_back_rather_than_crash():
    assert art.svg_for("euphoric") == art.svg_for("asleep")


# --- mascot state ----------------------------------------------------------

def test_no_session_means_asleep(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    assert p.mascot_state() == "asleep"


def test_a_running_session_is_neutral(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 60)
    assert p.mascot_state() == "neutral"


def test_drifting_shows_soft_alert_and_comes_back(conn, cfg, clock):
    """The state used to be set only when an alert fired, which left no way
    back: it went orange and stayed orange after you returned to the task."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 240)

    def labelled(app, on_task, minutes):
        eid = db.open_event(conn, Snapshot(ts=clock(), app=app, title=f"{app} w"), s.id)
        db.update_event_duration(conn, eid, minutes * 60)
        db.add_label(conn, eid, "rule", on_task, confidence=1.0, reason="test")

    labelled("YouTube", False, 11)
    assert p.mascot_state() == "soft-alert"

    labelled("Code", True, 5)
    assert p.mascot_state() == "neutral", "returning to the task must clear it"


def test_the_mascot_state_is_emitted_only_when_it_changes(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    events = p.start()
    assert _payload(events, pl.MASCOT)[0] == "asleep"

    assert pl.MASCOT not in _kinds(p.tick()), "no change, no event"

    p.sessions.start("fix the dataloader", 60)
    assert _payload(p.tick(), pl.MASCOT)[0] == "neutral"


# --- Pomodoro polish -------------------------------------------------------

def test_session_end_asks_how_it_went(conn, cfg, clock):
    """SPEC.md Feature 1: session end -> did you finish / partly / no.
    Phase 1 closed it silently, which skipped the question entirely."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 25)

    clock.advance(minutes=26)
    events = p.tick()
    assert _payload(events, pl.ASK_OUTCOME) == (s.id, "fix the dataloader")
    assert p.sessions.current is None, "the session ends whether or not we ask"


def test_the_outcome_is_recorded_after_the_session_closed(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 25)
    clock.advance(minutes=26)
    p.tick()

    p.record_outcome(s.id, "partly")
    assert conn.execute("SELECT outcome FROM sessions WHERE id=?",
                        (s.id,)).fetchone()[0] == "partly"


def test_a_nonsense_outcome_is_ignored_not_stored(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 25)
    p.record_outcome(s.id, "kind of")
    assert conn.execute("SELECT outcome FROM sessions WHERE id=?",
                        (s.id,)).fetchone()[0] is None


def test_an_exhausted_budget_still_ends_the_session_quietly(conn, cfg, clock):
    """Ending the session is unconditional — it is not gated on being allowed to
    interrupt. Since Phase 2 the outcome prompt is priority 90, so it clears the
    lower-priority suppressions and is asked through the arbiter."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 25)
    clock.advance(minutes=26)

    events = p.tick()
    assert p.sessions.current is None, "the session ends regardless"
    assert pl.ASK_OUTCOME in _kinds(events), "priority 90 clears the low gates"


# --- the capture gate ------------------------------------------------------

def _capture_cfg(cfg, **kwargs):
    return replace(cfg, capture=replace(cfg.capture, **kwargs))


def test_capture_ships_disabled(cfg):
    assert cfg.capture.enabled is False
    assert CaptureGate(cfg).allowed("Code", "main.py").allowed is False


def test_disabled_refuses_even_an_innocuous_window(cfg):
    verdict = CaptureGate(cfg).allowed("Code", "main.py")
    assert verdict.allowed is False and "disabled" in verdict.reason


@pytest.mark.parametrize("app,title", [
    ("1Password", "Personal vault"),
    ("Bitwarden", "My Vault"),
    ("keepassxc", "passwords.kdbx"),
    ("firefox", "Barclays Bank — online banking"),
    ("firefox", "Private Browsing"),
    ("chromium", "Incognito"),
    ("Signal", "a conversation"),
    ("pinentry-gnome3", "Enter passphrase"),
    ("Izy", "What are you working on?"),
])
def test_the_blocklist_refuses_what_you_would_regret(cfg, app, title):
    gate = CaptureGate(_capture_cfg(cfg, enabled=True))
    verdict = gate.allowed(app, title)
    assert verdict.allowed is False, f"{app} / {title} should be blocked"
    assert "blocklisted" in verdict.reason


def test_an_ordinary_window_is_allowed_when_enabled(cfg):
    gate = CaptureGate(_capture_cfg(cfg, enabled=True))
    assert gate.allowed("Code", "dataloader.py").allowed is True


def test_the_gate_fails_closed_on_an_unidentified_window(cfg):
    """'We could not tell what this was' is not a reason to photograph it."""
    gate = CaptureGate(_capture_cfg(cfg, enabled=True))
    assert gate.allowed(None, None).allowed is False
    assert gate.allowed("", "").allowed is False


def test_a_custom_blocklist_replaces_the_default(cfg):
    gate = CaptureGate(_capture_cfg(cfg, enabled=True, blocklist=("obsidian",)))
    assert gate.allowed("Obsidian", "notes").allowed is False
    # The default no longer applies once you have supplied your own.
    assert gate.allowed("1Password", "vault").allowed is True


def test_regex_entries_work_and_a_broken_one_fails_closed(cfg):
    gate = CaptureGate(_capture_cfg(cfg, enabled=True,
                                    blocklist=("re:^bank-", "re:[unclosed")))
    assert gate.allowed("bank-app", "x").allowed is False
    assert gate.allowed("Code", "main.py").allowed is True, \
        "a broken regex must be skipped, not crash the gate"


def test_the_default_blocklist_covers_the_obvious_categories():
    joined = " ".join(DEFAULT_BLOCKLIST)
    for needle in ("1password", "bitwarden", "keepass", "bank", "signal",
                   "incognito", "pinentry", "izy"):
        assert needle in joined


def test_the_backend_refuses_to_capture_silently(cfg):
    """There is no silent route on this platform, and pretending otherwise
    would be worse than not shipping the tier."""
    from izy.capture import PortalBackend
    with pytest.raises(NotImplementedError):
        PortalBackend().capture("/tmp/nope.png")


def test_doctor_describes_the_capture_state(cfg):
    assert "disabled" in describe(cfg)
    enabled = _capture_cfg(cfg, enabled=True)
    assert "disabled" not in describe(enabled)
