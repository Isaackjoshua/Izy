"""Title normalisation.

The churn case here was found in production, not in review: a terminal spinner
flipping every second wrote one activity_events row per poll, ~1-second spans,
which over a day is ~86k rows of noise and an unreadable timeline.
"""
from __future__ import annotations

import pytest

from izy.models import Snapshot, utcnow
from izy.titles import normalize_title
from izy.tracker import Tracker

from .conftest import snap


@pytest.mark.parametrize("raw,expected", [
    # the case measured in production
    ("◑ Implement markdown file requirements", "Implement markdown file requirements"),
    ("◐ Implement markdown file requirements", "Implement markdown file requirements"),
    ("⠹ Building project", "Building project"),
    ("⠧ Building project", "Building project"),
    ("(3) Inbox — Gmail", "Inbox — Gmail"),
    ("[12] general — Slack", "general — Slack"),
    ("Song Name - 1:23 / 4:56 - Spotify", "Song Name - Spotify"),
    ("Downloading 45% — Firefox", "Downloading — Firefox"),
    ("  spaced   out  ", "spaced out"),
])
def test_volatile_components_are_stripped(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "main.py — Izy",
    "dataloader.py — Izy",
    "PyTorch DataLoader docs — Mozilla Firefox",
    "a — b — c",
    "Issue #42: fix the thing",
    "3 Body Problem",                 # a leading digit that is not a counter
])
def test_meaningful_titles_are_left_alone(raw):
    assert normalize_title(raw) == raw


def test_none_and_empty_pass_through():
    assert normalize_title(None) is None
    assert normalize_title("") == ""


def test_a_title_that_is_only_a_spinner_is_kept():
    """Better a spinner frame than nothing — it still separates two activities."""
    assert normalize_title("◐") == "◐"


def test_spinner_frames_produce_one_span_not_one_per_poll(conn, clock):
    """The production regression, end to end."""
    frames = "◐◑◒◓"
    t = Tracker(conn, clock=clock)
    for i in range(40):
        t.tick(Snapshot(ts=clock(), app="gnome-terminal-server",
                        title=f"{frames[i % 4]} Implement markdown file requirements",
                        source="fake"))
        clock.advance(seconds=1)
    t.flush()

    rows = conn.execute("SELECT * FROM activity_events").fetchall()
    assert len(rows) == 1, f"40 spinner frames of one activity wrote {len(rows)} rows"
    assert rows[0]["window_title"] == "Implement markdown file requirements"
    assert rows[0]["duration_s"] >= 39


def test_a_real_window_change_still_splits(conn, clock):
    """Normalisation must not blunt genuine switches."""
    t = Tracker(conn, clock=clock)
    t.tick(snap(clock, app="Code", title="⠹ main.py"))
    clock.advance(seconds=10)
    t.tick(snap(clock, app="Code", title="⠧ test_main.py"))
    clock.advance(seconds=10)
    t.flush()

    titles = [r["window_title"] for r in
              conn.execute("SELECT * FROM activity_events ORDER BY id")]
    assert titles == ["main.py", "test_main.py"]
