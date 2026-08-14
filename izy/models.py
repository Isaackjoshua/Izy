"""Value types shared across the watcher, tracker and UI layers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """Timestamps are stored as ISO-8601 UTC text: sortable, readable in a
    sqlite3 shell, and unambiguous across DST and reboots."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass(frozen=True)
class Snapshot:
    """One sample of what the screen was showing. Produced by a Watcher.poll().

    `app` is a stable-ish identifier (wm_class or ActivityWatch's app name);
    `title` is the human-readable window title. `url` is only ever populated
    when a browser watcher is present — it stays None in Phase 1's native path.
    """

    ts: datetime
    app: str | None
    title: str | None
    url: str | None = None
    afk: bool = False
    source: str = "unknown"

    @property
    def key(self) -> tuple:
        """Identity of the activity span. A change here closes the open span
        and opens a new one; equal keys are the same continuous activity."""
        return (self.app, self.title, self.url, self.afk)

    def is_empty(self) -> bool:
        return not self.afk and not self.app and not self.title


@dataclass
class Session:
    id: int
    started_at: datetime
    declared_intent: str
    planned_minutes: int
    ended_at: datetime | None = None
    outcome: str | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


# Session outcomes, asked at session end (SPEC.md Feature 1).
OUTCOMES = ("finished", "partly", "no")

# Where a label came from. Phase 1 only ever writes "user".
LABEL_SOURCES = ("rule", "llm", "user")
