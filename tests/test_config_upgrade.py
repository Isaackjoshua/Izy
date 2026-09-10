"""Merging new config sections into a file the user already edited.

Every test here is really the same test: **their values survive.** This code
rewrites a file somebody owns, so the interesting cases are all the ways that
could go wrong, not the happy path.
"""
from __future__ import annotations

import tomllib

import pytest

from izy import config
from izy.config_upgrade import apply, parse, plan_for, upgrade_file

PHASE1_CONFIG = """\
# Izy config.

[watcher]
source = "auto"
poll_interval_s = 1.0
afk_timeout_s = 180
flush_interval_s = 15
activitywatch_url = "http://localhost:5600"

[session]
default_minutes = 25
break_minutes = 5
auto_close_after_minutes = 30

[self_label]
enabled = true
every_minutes = 60
active_from = "09:00"
active_until = "22:00"

[interruptions]
max_per_hour = 3
drift_min_minutes = 4
cooldown_after_dismiss_minutes = 15
deep_work_protect_minutes = 20

[mascot]
corner = "bottom-right"
margin_px = 24
dim_opacity = 0.35
proximity_px = 200
opacity = 0.9
"""


# --- the parser ------------------------------------------------------------

def test_parse_keeps_comments_with_what_they_introduce():
    sections = parse(config.DEFAULT_CONFIG_TOML)
    assert "classify" in sections
    header = "".join(sections["classify"].header)
    assert "tier 1" in header, "the section's explanation travels with it"
    key_block = "".join(sections["classify"].keys["min_duration_s"])
    assert "never classified" in key_block, "a key's comment travels with the key"


def test_parse_finds_every_shipped_section():
    assert set(parse(config.DEFAULT_CONFIG_TOML)) == set(config._SECTIONS)


# --- the plan --------------------------------------------------------------

def test_a_phase1_file_is_reported_as_missing_the_later_sections():
    plan = plan_for(PHASE1_CONFIG)
    assert set(plan.added_sections) == {"llm", "reminders", "classify",
                                        "drift", "capture"}
    assert plan.empty is False
    assert "llm" in plan.describe()


def test_the_shipped_default_needs_no_upgrade():
    assert plan_for(config.DEFAULT_CONFIG_TOML).empty is True


def test_a_missing_key_inside_an_existing_section_is_noticed():
    text = PHASE1_CONFIG.replace("afk_timeout_s = 180\n", "")
    assert "watcher.afk_timeout_s" in plan_for(text).added_keys


# --- applying it -----------------------------------------------------------

def test_upgrading_produces_valid_toml_with_every_section():
    upgraded, plan = apply(PHASE1_CONFIG)
    parsed = tomllib.loads(upgraded)
    assert set(parsed) == set(config._SECTIONS)
    assert plan_for(upgraded).empty is True, "upgrading twice is a no-op"


def test_your_edited_values_are_never_touched(tmp_path):
    """The whole point. An edited value must come through byte-identical."""
    edited = (PHASE1_CONFIG
              .replace("max_per_hour = 3", "max_per_hour = 1")
              .replace('corner = "bottom-right"', 'corner = "top-left"')
              .replace("poll_interval_s = 1.0", "poll_interval_s = 2.5"))
    upgraded, _ = apply(edited)
    parsed = tomllib.loads(upgraded)

    assert parsed["interruptions"]["max_per_hour"] == 1
    assert parsed["mascot"]["corner"] == "top-left"
    assert parsed["watcher"]["poll_interval_s"] == 2.5


def test_your_own_comments_survive():
    edited = PHASE1_CONFIG.replace(
        "max_per_hour = 3",
        "# lowered because 3 was too chatty for me\nmax_per_hour = 1")
    upgraded, _ = apply(edited)
    assert "# lowered because 3 was too chatty for me" in upgraded
    assert tomllib.loads(upgraded)["interruptions"]["max_per_hour"] == 1


def test_the_new_sections_arrive_with_their_comments():
    """A knob you cannot read about is a knob you cannot tune — the comments
    are the reason for doing this at all."""
    upgraded, _ = apply(PHASE1_CONFIG)
    assert "never invent" in upgraded or "asks you" in upgraded
    assert "blocklist" in upgraded
    assert "# Hard call ceilings" in upgraded


def test_a_missing_key_lands_inside_its_own_section():
    text = PHASE1_CONFIG.replace("afk_timeout_s = 180\n", "")
    upgraded, _ = apply(text)
    parsed = tomllib.loads(upgraded)
    assert parsed["watcher"]["afk_timeout_s"] == 180
    assert "afk_timeout_s" not in str(parsed.get("session", {}))


def test_upgrading_the_defaults_changes_nothing():
    upgraded, plan = apply(config.DEFAULT_CONFIG_TOML)
    assert plan.empty and upgraded == config.DEFAULT_CONFIG_TOML


def test_an_upgraded_phase1_file_loads_to_the_shipped_defaults(tmp_path):
    """After upgrading, an untouched file must behave exactly like a fresh one."""
    path = tmp_path / "config.toml"
    path.write_text(PHASE1_CONFIG)
    upgrade_file(path, backup=False)
    assert config.load(path) == config.Config()


def test_upgrade_keeps_a_backup(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(PHASE1_CONFIG)
    upgrade_file(path)
    assert (tmp_path / "config.toml.bak").read_text() == PHASE1_CONFIG


def test_upgrading_an_already_current_file_writes_nothing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.DEFAULT_CONFIG_TOML)
    plan = upgrade_file(path)
    assert plan.empty
    assert not (tmp_path / "config.toml.bak").exists(), "no backup for a no-op"


def test_a_file_with_only_one_section_still_upgrades():
    upgraded, plan = apply('[mascot]\ncorner = "top-left"\n')
    parsed = tomllib.loads(upgraded)
    assert parsed["mascot"]["corner"] == "top-left"
    assert set(parsed) == set(config._SECTIONS)


def test_an_empty_file_becomes_the_full_default():
    upgraded, _ = apply("")
    assert set(tomllib.loads(upgraded)) == set(config._SECTIONS)


def test_unknown_sections_of_your_own_are_left_alone():
    text = PHASE1_CONFIG + '\n[my_notes]\nsomething = "keep me"\n'
    upgraded, _ = apply(text)
    assert tomllib.loads(upgraded)["my_notes"]["something"] == "keep me"


# --- the command -----------------------------------------------------------

def test_config_command_reports_and_then_upgrades(tmp_path, monkeypatch, capsys):
    from izy import cli
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "config.toml").write_text(PHASE1_CONFIG)

    assert cli.main(["config"]) == 1, "out of date is a non-zero status"
    assert "izy config --upgrade" in capsys.readouterr().out

    assert cli.main(["config", "--upgrade"]) == 0
    out = capsys.readouterr().out
    assert "+ [classify]" in out and "+ [llm]" in out
    assert plan_for((tmp_path / "config.toml").read_text()).empty


def test_config_command_creates_a_missing_file(tmp_path, monkeypatch, capsys):
    from izy import cli
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["config"]) == 0
    assert "wrote a fresh config" in capsys.readouterr().out
    assert (tmp_path / "config.toml").exists()
