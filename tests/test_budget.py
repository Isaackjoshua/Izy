"""The interruption budget is a hard requirement, so it gets hard tests.

Every one of these encodes a line from SPEC.md Feature 4 that would otherwise
degrade into a guideline nobody enforces.
"""
from __future__ import annotations

from dataclasses import replace

from izy.budget import InterruptionBudget


def _budget(conn, cfg, clock, **overrides):
    if overrides:
        cfg = replace(cfg, interruptions=replace(cfg.interruptions, **overrides))
    return InterruptionBudget(conn, cfg, clock=clock)


def test_hourly_ceiling_is_a_hard_stop(conn, cfg, clock):
    b = _budget(conn, cfg, clock, max_per_hour=3)
    for _ in range(3):
        assert b.check("drift")[0] is True
        b.record("drift", response="acknowledged")
        clock.advance(minutes=1)

    allowed, reason = b.check("drift")
    assert allowed is False
    assert "ceiling" in reason


def test_ceiling_frees_up_once_the_hour_rolls_off(conn, cfg, clock):
    b = _budget(conn, cfg, clock, max_per_hour=1)
    b.record("drift", response="acknowledged")
    assert b.check("drift")[0] is False
    clock.advance(minutes=61)
    assert b.check("drift")[0] is True


def test_dismissal_starts_a_cooldown(conn, cfg, clock):
    b = _budget(conn, cfg, clock, max_per_hour=10,
                cooldown_after_dismiss_minutes=15)
    b.record("drift", response="dismissed")

    clock.advance(minutes=5)
    allowed, reason = b.check("drift")
    assert allowed is False and "cooldown" in reason

    clock.advance(minutes=11)
    assert b.check("drift")[0] is True


def test_deep_work_is_never_interrupted(conn, cfg, clock):
    b = _budget(conn, cfg, clock, deep_work_protect_minutes=20)
    allowed, reason = b.check("drift", deep_work_minutes=25)
    assert allowed is False and "deep-work" in reason
    assert b.check("drift", deep_work_minutes=19)[0] is True


def test_brief_context_switches_are_not_drift(conn, cfg, clock):
    b = _budget(conn, cfg, clock, drift_min_minutes=4)
    assert b.drift_qualifies(1.0) is False
    assert b.drift_qualifies(3.9) is False
    assert b.drift_qualifies(4.0) is True


def test_solicited_interactions_are_not_charged(conn, cfg, clock):
    """Clicking the mascot is not an interruption and must never be rationed."""
    b = _budget(conn, cfg, clock, max_per_hour=1)
    b.record("drift", response="dismissed")
    assert b.check("user_click")[0] is True


def test_budget_survives_a_restart(conn, cfg, clock):
    """History comes from the interventions table, not memory — a restart must
    not hand out a fresh allowance."""
    first = _budget(conn, cfg, clock, max_per_hour=2)
    first.record("drift", response="acknowledged")
    first.record("drift", response="acknowledged")

    restarted = _budget(conn, cfg, clock, max_per_hour=2)
    assert restarted.check("drift")[0] is False
