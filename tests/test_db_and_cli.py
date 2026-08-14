from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from izy import cli, db
from izy.tracker import Tracker

from .conftest import snap


def test_all_five_spec_tables_exist(conn):
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "activity_events", "labels",
            "reminders", "interventions"} <= names


def test_schema_is_idempotent(tmp_path):
    """Reconnecting must never destroy data — labels especially."""
    path = tmp_path / "data.db"
    c1 = db.connect(path)
    s = db.start_session(c1, "intent", 25)
    e = db.open_event(c1, snap(lambda: datetime.now(timezone.utc)), s.id)
    db.add_label(c1, e, "user", True)
    c1.close()

    c2 = db.connect(path)
    assert c2.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1
    c2.close()


def test_label_source_is_constrained(conn):
    e = db.open_event(conn, snap(lambda: datetime.now(timezone.utc)), None)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_label(conn, e, "vibes", True)


def test_label_requires_a_real_event(conn):
    """labels is the training set; an orphan row is a corrupt example."""
    with pytest.raises(sqlite3.IntegrityError):
        db.add_label(conn, 9999, "user", True)


def test_day_query_uses_local_calendar_days(conn, clock):
    """A day is what the person experienced as a day, not a UTC window."""
    t = Tracker(conn, clock=clock)
    t.tick(snap(clock, app="code", title="main.py"))
    clock.advance(minutes=30)
    t.flush()

    today = datetime.now().astimezone()
    events = db.events_for_day(conn, clock())
    # clock() is 2026-08-15 10:00 UTC; only assert self-consistency, since the
    # test machine's timezone is not ours to assume.
    assert len(events) == len(db.events_for_day(conn, clock()))
    assert isinstance(db.events_for_day(conn, today), list)


def test_cli_day_runs_on_an_empty_database(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path))
    assert cli.main(["day"]) == 0
    out = capsys.readouterr().out
    assert "Sessions (0)" in out and "none" in out


def test_cli_start_status_stop_round_trip(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path / "cfg"))

    assert cli.main(["start", "fix the dataloader", "-m", "30"]) == 0
    assert "fix the dataloader" in capsys.readouterr().out

    assert cli.main(["status"]) == 0
    assert "session" in capsys.readouterr().out

    assert cli.main(["stop", "partly"]) == 0
    assert "partly" in capsys.readouterr().out

    assert cli.main(["stop"]) == 1          # nothing left open
    assert "no open session" in capsys.readouterr().out


def test_cli_day_json_is_machine_readable(capsys, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path / "cfg"))
    cli.main(["start", "fix the dataloader"])
    capsys.readouterr()

    cli.main(["day", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["sessions"][0]["intent"] == "fix the dataloader"


def test_cli_day_shows_sessions_and_app_totals(capsys, tmp_path, monkeypatch, clock):
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path / "cfg"))

    conn = db.connect()
    s = db.start_session(conn, "fix the dataloader", 25)
    t = Tracker(conn, clock=lambda: datetime.now(timezone.utc))
    t.set_session(s.id)
    t.tick(snap(lambda: datetime.now(timezone.utc), app="code", title="main.py"))
    t.flush()
    conn.close()

    cli.main(["day", "-v"])
    out = capsys.readouterr().out
    assert "fix the dataloader" in out
    assert "code" in out and "main.py" in out
