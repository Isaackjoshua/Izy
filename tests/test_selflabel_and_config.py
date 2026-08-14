from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from izy import config, db
from izy.models import Snapshot
from izy.selflabel import SelfLabelPrompt
from izy.tracker import Tracker

from .conftest import FakeClock, snap


def _prompt(conn, cfg, clock, **overrides):
    if overrides:
        cfg = replace(cfg, self_label=replace(cfg.self_label, **overrides))
    return SelfLabelPrompt(conn, cfg, clock=clock)


def test_first_interval_is_not_ambushed(conn, cfg, clock):
    p = _prompt(conn, cfg, clock, every_minutes=60)
    assert p.due(False) is False        # arms the timer instead of firing
    clock.advance(minutes=59)
    assert p.due(False) is False
    clock.advance(minutes=2)
    assert p.due(False) is True


def test_never_asks_during_a_break(conn, cfg, clock):
    p = _prompt(conn, cfg, clock, every_minutes=1)
    p.due(False)
    clock.advance(minutes=5)
    assert p.due(True) is False
    assert p.due(False) is True


def test_respects_active_hours(conn, cfg):
    clock = FakeClock(datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc))
    p = _prompt(conn, cfg, clock, every_minutes=1,
                active_from="09:00", active_until="22:00")
    p.last_asked = clock()
    clock.advance(minutes=5)
    # 03:00 UTC is outside 09:00-22:00 local in any timezone west of UTC+6,
    # so assert the mechanism rather than a specific local hour.
    inside = p._within_active_hours(clock().astimezone())
    assert p.due(False) is inside


def test_disabled_never_asks(conn, cfg, clock):
    p = _prompt(conn, cfg, clock, enabled=False, every_minutes=1)
    clock.advance(minutes=90)
    assert p.due(False) is False


def test_picks_the_longest_recent_non_afk_event(conn, cfg, clock):
    """One interruption should buy the largest slice of labelled time."""
    t = Tracker(conn, clock=clock)
    p = _prompt(conn, cfg, clock)
    p.last_asked = clock()

    t.tick(snap(clock, app="slack", title="general"))
    clock.advance(minutes=2)
    t.tick(snap(clock, app="code", title="dataloader.py"))
    clock.advance(minutes=40)
    t.tick(snap(clock, app="afk", title=None, afk=True))
    t.flush()

    row = p.pick_event()
    assert row["window_title"] == "dataloader.py"


def test_answer_is_stored_as_user_ground_truth(conn, cfg, clock):
    t = Tracker(conn, clock=clock)
    t.tick(snap(clock, app="code", title="dataloader.py"))
    clock.advance(minutes=5)
    t.flush()
    event_id = conn.execute("SELECT id FROM activity_events").fetchone()[0]

    p = _prompt(conn, cfg, clock)
    p.record(event_id, on_task=True)

    row = conn.execute("SELECT * FROM labels").fetchone()
    assert row["source"] == "user" and row["on_task"] == 1 and row["confidence"] == 1.0


def test_skipping_costs_the_slot_and_is_logged_as_dismissed(conn, cfg, clock):
    """Re-asking after a dismissal is how one ignorable prompt becomes nagging."""
    p = _prompt(conn, cfg, clock, every_minutes=60)
    p.due(False)
    clock.advance(minutes=61)
    assert p.due(False) is True
    p.skip()
    assert p.due(False) is False

    row = conn.execute("SELECT * FROM interventions").fetchone()
    assert row["kind"] == "self_label" and row["user_response"] == "dismissed"


# --- config ----------------------------------------------------------------

def test_default_config_is_written_with_its_comments_intact(tmp_path):
    path = tmp_path / "config.toml"
    cfg = config.load(path)
    assert path.exists()
    text = path.read_text()
    assert "# Interruption budget" in text
    assert "prefer 0" in text, "the tuning rationale has to survive onto disk"
    assert cfg.interruptions.max_per_hour == 3


def test_written_default_config_parses_back_to_the_defaults(tmp_path):
    path = tmp_path / "config.toml"
    config.load(path)                      # writes it
    reloaded = config.load(path)           # parses it
    assert reloaded == config.Config(), "the shipped file must match the code defaults"


def test_partial_config_keeps_defaults_for_everything_else(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[interruptions]\nmax_per_hour = 1\n")
    cfg = config.load(path)
    assert cfg.interruptions.max_per_hour == 1
    assert cfg.interruptions.drift_min_minutes == 4
    assert cfg.session.default_minutes == 25


def test_unknown_keys_do_not_crash_startup(tmp_path):
    """A typo in a hand-edited config should not stop Izy from running."""
    path = tmp_path / "config.toml"
    path.write_text("[mascot]\ncorner = 'top-left'\nnonsense_key = 3\n")
    assert config.load(path).mascot.corner == "top-left"
