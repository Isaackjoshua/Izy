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

SCHEMA_VERSION = 3

#: Forward-only, numbered migrations (izy-v2.md §9). The base SCHEMA below is the
#: v2 baseline created idempotently; anything a later version adds lives here as
#: a numbered step, applied in order to a DB that predates it. A fresh DB gets
#: the base SCHEMA (= v2) and then every migration above 2, so both paths — new
#: install and upgrade — converge on the same shape. `izy doctor` reports the
#: version, and the writer backs the file up before applying any of these.
MIGRATIONS: dict[int, str] = {
    3: """
    -- Phase 2: every arbiter decision, so we can answer "why did it nag me at
    -- 14:02" and so the Reports screen can show an interruptions panel.
    CREATE TABLE IF NOT EXISTS interrupt_log (
        id           INTEGER PRIMARY KEY,
        ts           TEXT NOT NULL,       -- when the decision was made
        kind         TEXT NOT NULL,       -- drift | self_label | reminder | ...
        priority     INTEGER NOT NULL,
        dedupe_key   TEXT,
        requested_at TEXT NOT NULL,       -- when the request was first submitted
        verdict      TEXT NOT NULL,       -- show | defer | drop
        reason       TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_interrupt_log_ts   ON interrupt_log(ts);
    CREATE INDEX IF NOT EXISTS idx_interrupt_log_kind ON interrupt_log(kind, verdict);
    """,
}

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

-- Every LLM call, so spend is visible in the retrospective rather than a
-- surprise on a bill. Written by izy/llm.py and nowhere else.
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    cached        INTEGER NOT NULL DEFAULT 0,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);

-- Response cache. Identical questions must never be paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    key        TEXT PRIMARY KEY,
    purpose    TEXT NOT NULL,
    response   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect_readonly(path: Path | None = None) -> sqlite3.Connection:
    """A read-only view of the DB, for the API's GET handlers.

    izy-v2.md §1: the API never writes to the DB — the tick is the one writer.
    Opening with `mode=ro` enforces that at the SQLite level: any accidental
    write raises rather than silently creating a second writer. No schema script
    runs here, so this is a pure reader that also cannot race the migration."""
    path = path or db_path()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path is not None else paths.db_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # The base SCHEMA is the v2 baseline: idempotent creates for every table
    # that existed at v2. A brand-new DB has no stored version, so it is treated
    # as being at the baseline and then brought forward by the migrations below.
    stored = _stored_version(conn)
    conn.executescript(SCHEMA)
    _migrate(conn, path, baseline=2 if stored is None else stored)
    return conn


def _stored_version(conn) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else None
    except sqlite3.OperationalError:
        return None       # meta doesn't exist yet — a genuinely fresh file


def _migrate(conn, path, *, baseline: int) -> None:
    """Apply numbered migrations above `baseline`, backing the file up first."""
    pending = sorted(v for v in MIGRATIONS if v > baseline)
    if pending and str(path) != ":memory:":
        _backup(path, baseline)
    for version in pending:
        conn.executescript(MIGRATIONS[version])
    final = max([baseline, SCHEMA_VERSION, *pending])
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(final),))


def _backup(path, baseline: int) -> None:
    """Copy the DB before a migration touches it (izy-v2.md §9). Best-effort:
    a failed backup logs but does not block the upgrade, because refusing to
    start is worse than a missing copy of an already-committed WAL DB."""
    import shutil
    try:
        dest = Path(str(path) + f".premigrate-v{baseline}")
        if not dest.exists():
            shutil.copy2(path, dest)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("could not back up %s before migrating", path)


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


def latest_session(conn) -> Session | None:
    """The most recently started session, open or closed — used to answer an
    outcome that arrives after the session has already ended."""
    r = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
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
        # Store the normalised title: the exact spinner frame at the instant we
        # happened to sample carries no information, and this is the form the
        # Tier 3 cache key needs anyway.
        (session_id, to_iso(snap.ts), snap.app, snap.normalized_title,
         snap.url, int(snap.afk)),
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
              confidence: float | None = None, reason: str | None = None,
              *, now=None) -> int:
    cur = conn.execute(
        "INSERT INTO labels(event_id, source, on_task, confidence, reason, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (event_id, source, int(on_task), confidence, reason, to_iso(now or utcnow())),
    )
    return cur.lastrowid


def labels_for_day(conn, day: datetime) -> list[sqlite3.Row]:
    """Labels on that day's *activity* — not labels written that day. A batch
    flushed after midnight judges the previous day's windows."""
    lo, hi = _day_bounds(day)
    return conn.execute(
        "SELECT l.*, e.app, e.window_title FROM labels l"
        " JOIN activity_events e ON e.id = l.event_id"
        " WHERE e.ts >= ? AND e.ts < ? ORDER BY l.id",
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


# --- interrupt log (the arbiter's decisions) --------------------------------

def log_interrupt(conn, kind: str, priority: int, dedupe_key: str | None,
                  requested_at: datetime, verdict: str, reason: str,
                  *, now=None) -> int:
    cur = conn.execute(
        "INSERT INTO interrupt_log(ts, kind, priority, dedupe_key, requested_at,"
        " verdict, reason) VALUES (?,?,?,?,?,?,?)",
        (to_iso(now or utcnow()), kind, priority, dedupe_key,
         to_iso(requested_at), verdict, reason),
    )
    return cur.lastrowid


def interrupts_shown_since(conn, kind: str, since: datetime) -> int:
    """How many of one kind were actually shown since `since` — the arbiter's
    per-kind hourly cap and global cooldown are computed from this, so they
    survive a restart instead of resetting to a fresh allowance."""
    return conn.execute(
        "SELECT COUNT(*) FROM interrupt_log"
        " WHERE kind = ? AND verdict = 'show' AND ts >= ?",
        (kind, to_iso(since))).fetchone()[0]


def last_interrupt_shown(conn) -> datetime | None:
    row = conn.execute(
        "SELECT ts FROM interrupt_log WHERE verdict = 'show'"
        " ORDER BY id DESC LIMIT 1").fetchone()
    return from_iso(row["ts"]) if row else None


def interrupt_log_for_day(conn, day: datetime) -> list[sqlite3.Row]:
    lo, hi = _day_bounds(day)
    return conn.execute(
        "SELECT * FROM interrupt_log WHERE ts >= ? AND ts < ? ORDER BY id",
        (lo, hi)).fetchall()


# --- helpers ----------------------------------------------------------------

def day_bounds(day: datetime) -> tuple[str, str]:
    """Public alias — the report and CLI both need a day's UTC bounds."""
    return _day_bounds(day)


def _day_bounds(day: datetime) -> tuple[str, str]:
    """Local-calendar-day bounds, expressed as the UTC strings we store.

    A day is what the person experienced as a day, not a UTC window.
    """
    from datetime import timedelta

    local = day.astimezone() if day.tzinfo else day.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start), to_iso(start + timedelta(days=1))
