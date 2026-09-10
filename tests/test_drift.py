"""Drift alerts.

Every test here is a line from SPEC.md Feature 4. This is the only thing in Izy
that speaks unprompted about your work, and the failure mode for the whole
project is that it becomes annoying and gets closed, so the constraints are
tested as hard as the feature.
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
    return DriftDetector(conn, cfg, budget, clock=clock), budget


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


# --- when it fires ---------------------------------------------------------

def test_brief_context_switches_are_not_drift(conn, cfg, clock):
    """Four minutes is the floor; three is normal work."""
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=3)
    assert d.check(s.id, s.declared_intent) is None


def test_sustained_drift_alerts(conn, cfg, clock):
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    result = d.check(s.id, s.declared_intent)
    assert result is not None
    message, intervention_id = result
    assert message == "You said: fix the dataloader. YouTube, 11 min."
    assert intervention_id > 0


def test_deep_work_is_never_interrupted(conn, cfg, clock):
    """A 20-minute on-task streak outranks a drift alert."""
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4,
                     deep_work_protect_minutes=20)
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _labelled(conn, clock, s.id, "Code", True, minutes=25)
    assert d.state(s.id).deep_work_minutes >= 20
    assert d.check(s.id, s.declared_intent) is None


def test_the_hourly_ceiling_applies_to_drift(conn, cfg, clock):
    d, budget = _detector(conn, cfg, clock, drift_min_minutes=4, max_per_hour=1)
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    assert d.check(s.id, s.declared_intent) is not None
    assert d.check(s.id, s.declared_intent) is None, "the ceiling is hard"


def test_dismissal_starts_a_cooldown(conn, cfg, clock):
    """A dismissal buys 15 minutes of silence, and that silence outlasts the
    end of the run that caused it — a *new* drift run inside the cooldown is
    still suppressed."""
    d, budget = _detector(conn, cfg, clock, drift_min_minutes=4, max_per_hour=10,
                          cooldown_after_dismiss_minutes=15)
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    _, intervention_id = d.check(s.id, s.declared_intent)
    budget.resolve(intervention_id, "dismissed")

    # Back on task, then drifting again: a genuinely new run.
    _labelled(conn, clock, s.id, "Code", True, minutes=2)
    assert d.check(s.id, s.declared_intent) is None      # clears the per-run guard
    _labelled(conn, clock, s.id, "Reddit", False, minutes=6)

    clock.advance(minutes=5)
    assert d.check(s.id, s.declared_intent) is None, "still inside the cooldown"
    clock.advance(minutes=11)
    assert d.check(s.id, s.declared_intent) is not None


def test_no_session_means_no_alert(conn, cfg, clock):
    """Off a session there is no declared intent to drift from."""
    d, _ = _detector(conn, cfg, clock)
    assert d.check(None, None) is None


def test_disabled_in_config_never_alerts(conn, cfg, clock):
    cfg = replace(cfg, drift=replace(cfg.drift, enabled=False))
    budget = InterruptionBudget(conn, cfg, clock=clock)
    d = DriftDetector(conn, cfg, budget, clock=clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=30)
    assert d.check(s.id, s.declared_intent) is None


# --- how the streak is measured --------------------------------------------

def test_the_streak_is_the_trailing_run_only(conn, cfg, clock):
    """Earlier off-task time does not add to a run that on-task time broke."""
    d, _ = _detector(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=10)
    _labelled(conn, clock, s.id, "Code", True, minutes=10)
    _labelled(conn, clock, s.id, "YouTube", False, minutes=2)

    state = d.state(s.id)
    assert state.off_task_minutes == pytest.approx(2)


def test_being_away_breaks_the_streak_without_counting(conn, cfg, clock):
    """Walking away is not drift."""
    d, _ = _detector(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=10)
    _labelled(conn, clock, s.id, None, False, minutes=30, afk=True)

    state = d.state(s.id)
    assert state.off_task_minutes == 0 and state.deep_work_minutes == 0


def test_the_streak_survives_a_restart(conn, cfg, clock):
    """Computed from labelled events, not memory, so a restart mid-session does
    not reset a streak that really happened."""
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    fresh, _ = _detector(conn, cfg, clock, drift_min_minutes=4)
    assert fresh.check(s.id, s.declared_intent) is not None


def test_the_alert_names_the_app_that_pulled_you_out(conn, cfg, clock):
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4)
    s = db.start_session(conn, "write the report", 60, now=clock())
    _labelled(conn, clock, s.id, "Reddit", False, minutes=7)
    message, _ = d.check(s.id, s.declared_intent)
    assert message == "You said: write the report. Reddit, 7 min."


def test_one_alert_per_run_not_one_per_tick(conn, cfg, clock):
    """The drift check runs every second. Without a per-run guard a single
    stretch of drift spends the whole hourly ceiling in three ticks."""
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4, max_per_hour=3)
    s = db.start_session(conn, "fix the dataloader", 120, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)

    assert d.check(s.id, s.declared_intent) is not None
    for _ in range(20):                    # twenty more ticks of the same drift
        clock.advance(seconds=1)
        assert d.check(s.id, s.declared_intent) is None

    alerts = conn.execute(
        "SELECT COUNT(*) FROM interventions WHERE kind='drift'").fetchone()[0]
    assert alerts == 1, f"one continuous drift run produced {alerts} alerts"


def test_a_new_run_after_returning_to_task_can_alert_again(conn, cfg, clock):
    d, _ = _detector(conn, cfg, clock, drift_min_minutes=4, max_per_hour=3)
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _labelled(conn, clock, s.id, "YouTube", False, minutes=11)
    assert d.check(s.id, s.declared_intent) is not None

    _labelled(conn, clock, s.id, "Code", True, minutes=10)     # back on task
    assert d.check(s.id, s.declared_intent) is None            # clears the guard

    clock.advance(minutes=20)
    _labelled(conn, clock, s.id, "Reddit", False, minutes=8)   # drifts again
    assert d.check(s.id, s.declared_intent) is not None
