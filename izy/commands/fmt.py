"""Formatting shared by the command modules.\n\nSmall and dependency-free on purpose: every command prints durations and\nlocal times the same way."""
from __future__ import annotations

from datetime import datetime, timedelta

from ..models import from_iso

def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _local(ts: str) -> str:
    return from_iso(ts).astimezone().strftime("%H:%M:%S")


def _parse_day(s: str | None) -> datetime:
    if not s or s == "today":
        return datetime.now().astimezone()
    if s == "yesterday":
        return datetime.now().astimezone() - timedelta(days=1)
    return datetime.fromisoformat(s).astimezone()
