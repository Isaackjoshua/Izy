"""What survives of the budget after Phase 2: the drift threshold.

The interruption gating this file used to test — hourly caps, the dismiss
cooldown, deep-work protection — moved to the interrupt arbiter when v2 made it
the single dispatch point. Those rules are now tested in test_arbiter.py. All
that is left here is the one pure question drift detection and the mascot both
ask: has off-task time crossed the threshold?
"""
from __future__ import annotations

from dataclasses import replace

from izy.budget import InterruptionBudget


def _budget(conn, cfg, clock, **overrides):
    if overrides:
        cfg = replace(cfg, interruptions=replace(cfg.interruptions, **overrides))
    return InterruptionBudget(conn, cfg, clock=clock)


def test_brief_context_switches_are_not_drift(conn, cfg, clock):
    b = _budget(conn, cfg, clock, drift_min_minutes=4)
    assert b.drift_qualifies(1.0) is False
    assert b.drift_qualifies(3.9) is False
    assert b.drift_qualifies(4.0) is True
    assert b.drift_qualifies(11.0) is True


def test_the_threshold_follows_config(conn, cfg, clock):
    b = _budget(conn, cfg, clock, drift_min_minutes=10)
    assert b.drift_qualifies(9.0) is False
    assert b.drift_qualifies(10.0) is True
