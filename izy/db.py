"""SQLite storage. One file, WAL mode, no ORM.

The schema is SPEC.md's data model verbatim. All five tables are created now
even though Phase 1 only writes three of them, so later phases add rows rather
than migrate structure.

`labels` is the training set. Every user correction is a labeled example.
Nothing in this module deletes from it, and nothing ever should.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from . import paths
from .models import Session, Snapshot, from_iso, to_iso, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    declared_intent TEXT NOT NULL,
    planned_minutes INTEGER NOT NULL,
    outcome         TEXT
);

CREATE TABLE IF NOT EXISTS activity_events (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES sessions(id),
    ts           TEXT NOT NULL,
    app          TEXT,
    window_title TEXT,
    url          TEXT,
    duration_s   REAL NOT NULL DEFAULT 0,
    afk          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON activity_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON activity_events(session_id);

CREATE TABLE IF NOT EXISTS labels (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES activity_events(id),
    source     TEXT NOT NULL CHECK (source IN ('rule','llm','user')),
    on_task    INTEGER NOT NULL,
    confidence REAL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_event ON labels(event_id);

CREATE TABLE IF NOT EXISTS reminders (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    parsed_kind     TEXT CHECK (parsed_kind IN ('time','context')),
    due_at          TEXT,
    trigger_context TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','fired','done','dismissed','snoozed')),
    fired_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_status ON reminders(status);

CREATE TABLE IF NOT EXISTS interventions (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    message       TEXT,
    user_response TEXT CHECK (user_response IN ('dismissed','acknowledged','snoozed'))
);
CREATE INDEX IF NOT EXISTS idx_interventions_ts ON interventions(ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or paths.db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    return conn


# --- sessions ---------------------------------------------------------------

def start_session(conn, intent: str, planned_minutes: int, *, now=None) -> Session:
    now = now or utcnow()
    cur = conn.execute(
        "INSERT INTO sessions(started_at, declared_intent, planned_minutes) VALUES (?,?,?)",
        (to_iso(now), intent, planned_minutes),
    )
    return Session(cur.lastrowid, now, intent, planned_minutes)


def end_session(conn, session_id: int, outcome: str | None = None, *, now=None) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at=?, outcome=? WHERE id=? AND ended_at IS NULL",
        (to_iso(now or utcnow()), outcome, session_id),
    )


def set_outcome(conn, session_id: int, outcome: str) -> None:
    conn.execute("UPDATE sessions SET outcome=? WHERE id=?", (outcome, session_id))


def _row_to_session(r) -> Session:
    return Session(
        id=r["id"],
        started_at=from_iso(r["started_at"]),
        declared_intent=r["declared_intent"],
        planned_minutes=r["planned_minutes"],
        ended_at=from_iso(r["ended_at"]) if r["ended_at"] else None,
        outcome=r["outcome"],
    )


def open_session(conn) -> Session | None:
    r = conn.execute(
        "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _row_to_session(r) if r else None


def sessions_for_day(conn, day: datetime) -> list[Session]:
    lo, hi = _day_bounds(day)
    rows = conn.execute(
        "SELECT * FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at",
        (lo, hi),
    ).fetchall()
    return [_row_to_session(r) for r in rows]


# --- activity events --------------------------------------------------------

def open_event(conn, snap: Snapshot, session_id: int | None) -> int:
    cur = conn.execute(
        "INSERT INTO activity_events(session_id, ts, app, window_title, url, duration_s, afk)"
        " VALUES (?,?,?,?,?,0,?)",
        (session_id, to_iso(snap.ts), snap.app, snap.title, snap.url, int(snap.afk)),
    )
    return cur.lastrowid


def update_event_duration(conn, event_id: int, duration_s: float) -> None:
    conn.execute("UPDATE activity_events SET duration_s=? WHERE id=?", (duration_s, event_id))


def events_for_day(conn, day: datetime) -> list[sqlite3.Row]:
    lo, hi = _day_bounds(day)
    return conn.execute(
        "SELECT * FROM activity_events WHERE ts >= ? AND ts < ? ORDER BY ts", (lo, hi)
    ).fetchall()


def recent_events(conn, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM activity_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


# --- labels -----------------------------------------------------------------

def add_label(conn, event_id: int, source: str, on_task: bool,
              confidence: float | None = None, reason: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO labels(event_id, source, on_task, confidence, reason, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (event_id, source, int(on_task), confidence, reason, to_iso(utcnow())),
    )
    return cur.lastrowid


def labels_for_day(conn, day: datetime) -> list[sqlite3.Row]:
    lo, hi = _day_bounds(day)
    return conn.execute(
        "SELECT l.*, e.app, e.window_title FROM labels l"
        " JOIN activity_events e ON e.id = l.event_id"
        " WHERE l.created_at >= ? AND l.created_at < ? ORDER BY l.created_at",
        (lo, hi),
    ).fetchall()


# --- interventions ----------------------------------------------------------

def record_intervention(conn, kind: str, message: str | None = None,
                        response: str | None = None, *, now=None) -> int:
    cur = conn.execute(
        "INSERT INTO interventions(ts, kind, message, user_response) VALUES (?,?,?,?)",
        (to_iso(now or utcnow()), kind, message, response),
    )
    return cur.lastrowid


def set_intervention_response(conn, intervention_id: int, response: str) -> None:
    conn.execute(
        "UPDATE interventions SET user_response=? WHERE id=?", (response, intervention_id)
    )


def interventions_since(conn, since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM interventions WHERE ts >= ? ORDER BY ts", (to_iso(since),)
    ).fetchall()


# --- helpers ----------------------------------------------------------------

def _day_bounds(day: datetime) -> tuple[str, str]:
    """Local-calendar-day bounds, expressed as the UTC strings we store.

    A day is what the person experienced as a day, not a UTC window.
    """
    from datetime import timedelta

    local = day.astimezone() if day.tzinfo else day.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start), to_iso(start + timedelta(days=1))
