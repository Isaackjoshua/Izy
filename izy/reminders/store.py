"""Reminder persistence. The `reminders` table has existed since Phase 1's
schema; this is what finally writes to it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import from_iso, to_iso, utcnow
from .parse import ParsedReminder

STATUSES = ("pending", "fired", "done", "dismissed", "snoozed")
#: Statuses that can still fire. `fired` is terminal-ish but stays until
#: answered, so it is not re-fired.
LIVE = ("pending", "snoozed")


@dataclass
class Reminder:
    id: int
    created_at: datetime
    raw_text: str
    parsed_kind: str | None
    due_at: datetime | None
    trigger_context: str | None
    status: str
    fired_at: datetime | None = None

    @property
    def text(self) -> str:
        """What the bubble shows. Falls back to the raw text for rows written
        before a body was extracted."""
        return self.raw_text


def _row(r) -> Reminder:
    return Reminder(
        id=r["id"],
        created_at=from_iso(r["created_at"]),
        raw_text=r["raw_text"],
        parsed_kind=r["parsed_kind"],
        due_at=from_iso(r["due_at"]) if r["due_at"] else None,
        trigger_context=r["trigger_context"],
        status=r["status"],
        fired_at=from_iso(r["fired_at"]) if r["fired_at"] else None,
    )


def add(conn, parsed: ParsedReminder, *, now=None) -> Reminder:
    """Store a parsed reminder. Refuses one that isn't actionable — a row with
    neither a time nor a trigger would never fire and would rot silently."""
    if not parsed.is_valid():
        raise ValueError("reminder has neither a due time nor a trigger context")
    now = now or utcnow()
    cur = conn.execute(
        "INSERT INTO reminders(created_at, raw_text, parsed_kind, due_at,"
        " trigger_context, status) VALUES (?,?,?,?,?, 'pending')",
        (to_iso(now), parsed.text, parsed.kind,
         to_iso(parsed.due_at) if parsed.due_at else None,
         parsed.trigger_context),
    )
    return get(conn, cur.lastrowid)


def get(conn, reminder_id: int) -> Reminder | None:
    r = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return _row(r) if r else None


def pending(conn) -> list[Reminder]:
    """Everything still waiting to fire, soonest first. `list reminders`."""
    rows = conn.execute(
        f"SELECT * FROM reminders WHERE status IN ({','.join('?' * len(LIVE))})"
        " ORDER BY COALESCE(due_at, '9999') ASC, id ASC", LIVE).fetchall()
    return [_row(r) for r in rows]


def due_by_time(conn, *, now=None) -> list[Reminder]:
    now = now or utcnow()
    rows = conn.execute(
        f"SELECT * FROM reminders WHERE parsed_kind = 'time'"
        f" AND status IN ({','.join('?' * len(LIVE))})"
        " AND due_at IS NOT NULL AND due_at <= ? ORDER BY due_at",
        (*LIVE, to_iso(now))).fetchall()
    return [_row(r) for r in rows]


def due_by_context(conn, context: str) -> list[Reminder]:
    rows = conn.execute(
        f"SELECT * FROM reminders WHERE parsed_kind = 'context'"
        f" AND status IN ({','.join('?' * len(LIVE))})"
        " AND trigger_context = ? ORDER BY id", (*LIVE, context)).fetchall()
    return [_row(r) for r in rows]


def mark_fired(conn, reminder_id: int, *, now=None) -> None:
    conn.execute("UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
                 (to_iso(now or utcnow()), reminder_id))


def mark_done(conn, reminder_id: int) -> None:
    conn.execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))


def mark_dismissed(conn, reminder_id: int) -> None:
    conn.execute("UPDATE reminders SET status='dismissed' WHERE id=?", (reminder_id,))


def snooze(conn, reminder_id: int, minutes: int, *, now=None) -> datetime:
    """Push a reminder out. A snoozed context reminder becomes time-based —
    'in 10 minutes' is a time, whatever triggered it originally."""
    now = now or utcnow()
    due = now + timedelta(minutes=minutes)
    conn.execute(
        "UPDATE reminders SET status='snoozed', parsed_kind='time', due_at=?"
        " WHERE id=?", (to_iso(due), reminder_id))
    return due
