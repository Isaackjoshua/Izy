"""Drift detection.

Since Phase 2 this module only *detects* drift — the message when off-task time
crosses the threshold, at most once per continuous run. Whether that message
reaches the screen (deep-work, caps, cooldown, one-at-a-time) is the arbiter's
job now, tested in test_arbiter.py. So these tests are about the two things
detection still owns: the streak measurement and the once-per-run guard.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from izy import db
from izy.budget import InterruptionBudget
from izy.drift import DriftDetector, format_alert
from izy.models import Snapshot


def _detector(conn, cfg, clock, **interruptions):
    if interruptions:
        cfg = replace(cfg, interruptions=replace(cfg.interruptions, **interruptions))
    budget = InterruptionBudget(conn, cfg, clock=clock)
    return DriftDetector(conn, cfg, budget, clock=clock)


def _labelled(conn, clock, session_id, app, on_task, minutes, afk=False):
    snap = Snapshot(ts=clock(), app=app, title=f"{app} window", afk=afk)
    eid = db.open_event(conn, snap, session_id)
    db.update_event_duration(conn, eid, minutes * 60)
    if not afk:
        db.add_label(conn, eid, "rule", on_task, confidence=1.0, reason="test")
    return eid


# --- the message -----------------------------------------------------------

def test_the_alert_says_the_intent_and_nothing_more():
    assert format_alert("fix the dataloader", "YouTube", 11) == \
        "You said: fix the dataloader. YouTube, 11 min."


def test_the_alert_carries_no_encouragement_or_guilt():
    msg = format_alert("fix the dataloader", "YouTube", 11)
    assert "!" not in msg
    assert all(ord(c) < 0x2100 for c in msg), "no emoji"
    for word in ("should", "try", "focus!", "come on", "back on track", "good"):
        assert word not in msg.lower()


# --- detection -------------------------------------------------------------

def test_brief_context_switches_are_not_drift(conn, cfg, clock):
    """Four minutes is the floor; three is normal work."""
    d = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=3)
    assert d.detect(s.id, s.declared_intent) is None


def test_sustained_drift_returns_the_message(conn, cfg, clock):
    d = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)
    assert d.detect(s.id, s.declared_intent) == \
        "You said: fix the dataloader. YouTube, 11 min."


def test_no_session_means_no_detection(conn, cfg, clock):
    d = _detector(conn, cfg, clock)
    assert d.detect(None, None) is None


def test_disabled_in_config_never_detects(conn, cfg, clock):
    cfg = replace(cfg, drift=replace(cfg.drift, enabled=False))
    d = DriftDetector(conn, cfg, InterruptionBudget(conn, cfg, clock=clock), clock=clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=30)
    assert d.detect(s.id, s.declared_intent) is None


def test_the_alert_names_the_app_that_pulled_you_out(conn, cfg, clock):
    d = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "write the report", 60, now=clock())
    _labelled(conn, clock, s.id, "Reddit", False, minutes=7)
    assert d.detect(s.id, s.declared_intent) == \
        "You said: write the report. Reddit, 7 min."


# --- the once-per-run guard ------------------------------------------------

def test_one_message_per_run_not_one_per_tick(conn, cfg, clock):
    """detect() runs every tick; without the guard a single stretch of drift
    would submit an identical request every second."""
    d = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    assert d.detect(s.id, s.declared_intent) is not None
    for _ in range(20):
        clock.advance(seconds=1)
        assert d.detect(s.id, s.declared_intent) is None, "one message per run"


def test_a_new_run_after_returning_to_task_detects_again(conn, cfg, clock):
    d = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)
    assert d.detect(s.id, s.declared_intent) is not None

    _labelled(conn, clock, s.id, "Code", True, minutes=10)     # back on task
    assert d.detect(s.id, s.declared_intent) is None           # clears the guard

    clock.advance(minutes=20)
    _labelled(conn, clock, s.id, "Reddit", False, minutes=8)   # drifts again
    assert d.detect(s.id, s.declared_intent) is not None


# --- how the streak is measured --------------------------------------------

def test_the_streak_is_the_trailing_run_only(conn, cfg, clock):
    d = _detector(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=10)
    _labelled(conn, clock, s.id, "Code", True, minutes=10)
    _labelled(conn, clock, s.id, "YouTube", False, minutes=2)
    assert d.state(s.id).off_task_minutes == pytest.approx(2)


def test_deep_work_streak_is_measured(conn, cfg, clock):
    """The arbiter reads this to protect deep work; drift only reports it."""
    d = _detector(conn, cfg, clock, deep_work_protect_minutes=20)
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _labelled(conn, clock, s.id, "Code", True, minutes=25)
    assert d.state(s.id).deep_work_minutes >= 20
    # no off-task run, so nothing to detect
    assert d.detect(s.id, s.declared_intent) is None


def test_being_away_breaks_the_streak_without_counting(conn, cfg, clock):
    d = _detector(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=10)
    _labelled(conn, clock, s.id, None, False, minutes=30, afk=True)
    state = d.state(s.id)
    assert state.off_task_minutes == 0 and state.deep_work_minutes == 0


def test_the_streak_survives_a_restart(conn, cfg, clock):
    """Computed from labelled events, not memory."""
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)
    fresh = _detector(conn, cfg, clock, drift_min_minutes=4)
    assert fresh.detect(s.id, s.declared_intent) is not None
