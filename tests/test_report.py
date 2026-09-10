"""The retrospective dashboard.

Split so the numbers are checked without parsing HTML: `data.py` is pure
arithmetic over SQLite, `render.py` turns that into a page, and the server is
exercised over real HTTP against a real SQLite file.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import timedelta

import pytest

from izy import db
from izy.models import Snapshot, from_iso
from izy.report import build, render, write
from izy.report.data import AFK, BREAK, OFF, ON, UNJUDGED
from izy.report.render import fmt_duration


def _event(conn, clock, app, title, seconds, session_id=None, on_task=None,
           afk=False, source="rule", reason="test", confidence=1.0):
    eid = db.open_event(conn, Snapshot(ts=clock(), app=app, title=title, afk=afk),
                        session_id)
    db.update_event_duration(conn, eid, seconds)
    if on_task is not None:
        db.add_label(conn, eid, source, on_task, confidence=confidence, reason=reason)
    clock.advance(seconds=seconds)
    return eid


# --- data ------------------------------------------------------------------

def test_an_empty_day_reports_nothing_rather_than_crashing(conn, cfg, clock):
    r = build(conn, cfg, clock())
    assert r.has_data is False
    assert r.on_task_share is None
    assert "No activity recorded" in render(r)


def test_totals_split_by_band(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, "YouTube", "a video", 300, s.id, on_task=False)
    _event(conn, clock, None, None, 900, s.id, afk=True)

    r = build(conn, cfg, clock())
    assert r.totals[ON] == 600 and r.totals[OFF] == 300 and r.totals[AFK] == 900
    assert r.on_task_share == pytest.approx(600 / 900)


def test_away_is_excluded_from_the_on_task_share(conn, cfg, clock):
    """Being away is neither on nor off task, so it must not move the number."""
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    before = build(conn, cfg, clock()).on_task_share
    _event(conn, clock, None, None, 3600, s.id, afk=True)
    assert build(conn, cfg, clock()).on_task_share == before


def test_unjudged_events_are_shown_as_unjudged_not_as_on_task(conn, cfg, clock):
    """An event awaiting tier 4 must never be quietly counted as fine."""
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _event(conn, clock, "Obsidian", "notes", 600, s.id)   # no label
    r = build(conn, cfg, clock())
    assert r.totals[UNJUDGED] == 600
    assert r.totals[ON] == 0 and r.judged_seconds == 0


def test_the_latest_label_wins(conn, cfg, clock):
    """A user correction supersedes the machine's verdict."""
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    eid = _event(conn, clock, "Obsidian", "notes", 600, s.id, on_task=False,
                 source="llm", confidence=0.8)
    db.add_label(conn, eid, "user", True, confidence=1.0, reason="you said so")
    r = build(conn, cfg, clock())
    assert r.totals[ON] == 600 and r.totals[OFF] == 0


def test_drift_starts_name_the_app_that_pulled_you_out(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, "YouTube", "a video", 300, s.id, on_task=False)
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, "YouTube", "another", 300, s.id, on_task=False)
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, "Reddit", "a thread", 200, s.id, on_task=False)

    r = build(conn, cfg, clock())
    counts = {app: count for app, count, _ in r.drift_starts}
    assert counts == {"YouTube": 2, "Reddit": 1}
    assert r.drift_starts[0][0] == "YouTube", "most time lost first"
    assert r.drift_starts[0][2] == 600, "both YouTube runs charged to YouTube"


def test_a_continuing_off_task_run_counts_as_one_drift(conn, cfg, clock):
    """Drift is the moment attention left, not every span after it."""
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, "YouTube", "one", 300, s.id, on_task=False)
    _event(conn, clock, "YouTube", "two", 300, s.id, on_task=False)
    _event(conn, clock, "Reddit", "three", 300, s.id, on_task=False)

    r = build(conn, cfg, clock())
    assert [(a, c) for a, c, _ in r.drift_starts] == [("YouTube", 1)]
    assert r.drift_starts[0][2] == 900, \
        "the whole run is charged to the app that started it, not split"


def test_away_breaks_a_run_so_returning_off_task_is_new_drift(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    _event(conn, clock, None, None, 1800, s.id, afk=True)
    _event(conn, clock, "YouTube", "a video", 300, s.id, on_task=False)
    r = build(conn, cfg, clock())
    assert r.drift_starts == [], "after being away there was no on->off transition"


def test_weakest_hours_carry_their_sample_size(conn, cfg, clock):
    """A 100% figure from forty seconds is not a pattern, and the page has to
    be able to say so."""
    s = db.start_session(conn, "fix the dataloader", 240, now=clock())
    _event(conn, clock, "YouTube", "a video", 40, s.id, on_task=False)
    r = build(conn, cfg, clock())
    assert len(r.weakest_hours) == 1
    hour, share, judged = r.weakest_hours[0]
    assert share == 1.0 and judged == 40


def test_break_bands_are_derived_from_ended_sessions(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    db.end_session(conn, s.id, "finished", now=clock())

    r = build(conn, cfg, clock())
    breaks = [b for b in r.bands if b.kind == BREAK]
    assert len(breaks) == 1
    assert breaks[0].seconds == cfg.session.break_minutes * 60


def test_audit_carries_only_tier_3_and_4_decisions(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _event(conn, clock, "Code", "main.py", 60, s.id, on_task=True, source="rule")
    _event(conn, clock, "Obsidian", "notes", 60, s.id, on_task=True, source="llm",
           confidence=0.9, reason="looks related")
    _event(conn, clock, "Zoom", "standup", 60, s.id, on_task=True, source="user")

    r = build(conn, cfg, clock())
    assert {a["source"] for a in r.audit} == {"llm", "user"}
    assert any(a["reason"] == "looks related" for a in r.audit)


def test_llm_spend_separates_paid_calls_from_cache_hits(conn, cfg, clock):
    from izy.llm import LLM
    from tests.test_llm import FakeClient
    llm = LLM(conn, cfg, client=FakeClient({"x": "1"}), clock=clock)
    llm.complete_json("p", "a", {"type": "object"})
    llm.complete_json("p", "a", {"type": "object"})   # cached

    r = build(conn, cfg, clock())
    assert r.llm["calls"] == 1 and r.llm["cache_hits"] == 1
    assert r.llm["cost_usd"] > 0


# --- rendering -------------------------------------------------------------

def _full_day(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    _event(conn, clock, "Code", "main.py", 1800, s.id, on_task=True)
    _event(conn, clock, "YouTube", "a video", 600, s.id, on_task=False,
           source="llm", confidence=0.82, reason="not related to the dataloader")
    _event(conn, clock, None, None, 900, s.id, afk=True)
    db.end_session(conn, s.id, "partly", now=clock())
    return build(conn, cfg, clock())


def test_the_page_is_self_contained(conn, cfg, clock):
    """Local-first: it must render with the machine offline."""
    page = render(_full_day(conn, cfg, clock))
    assert "http://" not in page.replace("http://127.0.0.1", "")
    assert "https://" not in page
    assert "<script src" not in page and "<link" not in page


def test_the_page_shows_every_section_spec_requires(conn, cfg, clock):
    page = render(_full_day(conn, cfg, clock))
    for heading in ("Timeline", "Sessions", "What pulled you out",
                    "When you are weakest", "Classification audit", "LLM spend"):
        assert heading in page, f"missing section: {heading}"


def test_the_audit_offers_a_one_click_correction(conn, cfg, clock):
    page = render(_full_day(conn, cfg, clock))
    assert "This was wrong" in page
    assert "/relabel" in page


def test_identity_is_never_carried_by_colour_alone(conn, cfg, clock):
    """Aqua is below 3:1 on the light surface, so the relief rule applies: each
    band kind is named in the legend and again in the totals table."""
    page = render(_full_day(conn, cfg, clock))
    for label in ("On task", "Off task", "Away"):
        assert page.count(label) >= 2, f"{label} should appear as text twice"


def test_session_row_shows_planned_against_actual(conn, cfg, clock):
    page = render(_full_day(conn, cfg, clock))
    assert "fix the dataloader" in page
    assert "60m" in page and "partly" in page


def test_titles_are_escaped(conn, cfg, clock):
    """Window titles are untrusted text — they are whatever was on screen."""
    s = db.start_session(conn, "x", 60, now=clock())
    _event(conn, clock, "Code", "<script>alert(1)</script>", 600, s.id,
           on_task=False, source="llm", confidence=0.9)
    page = render(build(conn, cfg, clock()))
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_an_intent_with_quotes_does_not_break_the_page(conn, cfg, clock):
    s = db.start_session(conn, 'fix the "dataloader" & thing', 60, now=clock())
    _event(conn, clock, "Code", "main.py", 600, s.id, on_task=True)
    page = render(build(conn, cfg, clock()))
    assert "&amp;" in page and 'fix the "dataloader"' not in page


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (45, "45s"), (60, "1m"), (599, "9m"), (3600, "1h 00m"),
    (5430, "1h 30m"),
])
def test_durations_read_naturally(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_write_puts_the_file_where_it_says(conn, cfg, clock, tmp_path):
    path = write(conn, cfg, clock(), path=tmp_path / "day.html")
    assert path.exists() and path.read_text().startswith("<!doctype html>")


# --- the server ------------------------------------------------------------

def test_the_server_serves_the_page_and_saves_a_correction(tmp_path, cfg, clock):
    from izy.report.serve import ReportServer

    db_path = tmp_path / "data.db"
    conn = db.connect(db_path)
    s = db.start_session(conn, "fix the dataloader", 60, now=clock())
    eid = _event(conn, clock, "Obsidian", "notes", 600, s.id, on_task=False,
                 source="llm", confidence=0.8, reason="unclear")
    conn.close()

    server = ReportServer(lambda: db.connect(db_path), cfg)
    server.start()
    try:
        page = urllib.request.urlopen(server.url, timeout=5).read().decode()
        assert "Classification audit" in page

        req = urllib.request.Request(
            server.url + "relabel", method="POST",
            data=json.dumps({"event_id": eid, "on_task": True}).encode(),
            headers={"Content-Type": "application/json"})
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["ok"]
    finally:
        server.stop()

    conn = db.connect(db_path)
    latest = conn.execute(
        "SELECT source, on_task FROM labels WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (eid,)).fetchone()
    assert latest["source"] == "user" and latest["on_task"] == 1
    conn.close()


def test_the_server_binds_only_to_loopback(tmp_path, cfg):
    """Nothing in Izy should ever be reachable from the network."""
    from izy.report.serve import ReportServer
    server = ReportServer(lambda: db.connect(tmp_path / "d.db"), cfg)
    try:
        assert server._httpd.server_address[0] == "127.0.0.1"
    finally:
        server._httpd.server_close()


def test_the_server_rejects_unknown_routes_and_bad_bodies(tmp_path, cfg):
    from izy.report.serve import ReportServer
    server = ReportServer(lambda: db.connect(tmp_path / "d.db"), cfg)
    server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(server.url + "../etc/passwd", timeout=5)
        assert e.value.code == 404

        req = urllib.request.Request(
            server.url + "relabel", method="POST", data=b"not json",
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 400
    finally:
        server.stop()
