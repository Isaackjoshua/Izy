"""Turns a stream of point-in-time snapshots into durable activity spans.

Deliberately pure: no Qt, no threads, no wall-clock reads except through the
injected `clock`. That makes a day of tracking reproducible in a unit test in
milliseconds, which is the only practical way to check the boundary conditions
(staleness, session changes, restart recovery) that matter here.

One row per *span*, not per poll. A poll at 1 Hz would be ~86k rows a day of
almost entirely duplicate data; coalescing on (app, title, url, afk) gives a
few hundred rows that mean something, and matches the shape SPEC.md's
`activity_events.duration_s` column already implies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .models import Snapshot, utcnow

log = logging.getLogger(__name__)


@dataclass
class OpenSpan:
    event_id: int
    key: tuple
    started_at: datetime
    last_seen: datetime
    session_id: int | None


class Tracker:
    """Consumes snapshots, writes activity_events.

    Args:
        conn: open SQLite connection.
        flush_interval_s: how often an in-progress span's duration is written,
            bounding what a crash can lose.
        stale_after_s: if the watcher returns nothing for this long, the open
            span is closed at its last confirmed sighting rather than being
            credited with time we cannot vouch for. Guessing here would quietly
            inflate every "hours focused" number the retrospective ever shows.
    """

    def __init__(self, conn, *, flush_interval_s: float = 15.0,
                 stale_after_s: float = 60.0, clock=utcnow) -> None:
        self.conn = conn
        self.flush_interval_s = flush_interval_s
        self.stale_after_s = stale_after_s
        self.clock = clock
        self.span: OpenSpan | None = None
        self._last_flush: datetime | None = None
        self._session_id: int | None = None

    # --- session coupling --------------------------------------------------

    def set_session(self, session_id: int | None) -> None:
        """Session boundaries are also span boundaries, so an event never
        straddles two sessions and 'time spent on this intent' stays exact."""
        if session_id == self._session_id:
            return
        self._close_span(self.clock())
        self._session_id = session_id

    # --- main loop ---------------------------------------------------------

    def tick(self, snap: Snapshot | None) -> None:
        now = self.clock()
        if snap is None:
            self._handle_gap(now)
            return

        if self.span is None:
            self._open_span(snap)
            return

        if snap.key != self.span.key:
            self._close_span(snap.ts)
            self._open_span(snap)
            return

        self.span.last_seen = snap.ts
        if self._last_flush is None or \
                (now - self._last_flush).total_seconds() >= self.flush_interval_s:
            self._flush(now)

    def _handle_gap(self, now: datetime) -> None:
        if self.span is None:
            return
        if (now - self.span.last_seen).total_seconds() >= self.stale_after_s:
            log.debug("watcher stale for %.0fs, closing span at last sighting",
                      self.stale_after_s)
            self._close_span(self.span.last_seen)

    def flush(self) -> None:
        """Write the open span's duration now. Called before shutdown."""
        if self.span is not None:
            self._flush(self.clock())

    def stop(self) -> None:
        """Close everything cleanly. The next start recovers from the DB."""
        self._close_span(self.clock())

    # --- span bookkeeping --------------------------------------------------

    def _open_span(self, snap: Snapshot) -> None:
        event_id = db.open_event(self.conn, snap, self._session_id)
        self.span = OpenSpan(event_id, snap.key, snap.ts, snap.ts, self._session_id)
        self._last_flush = snap.ts

    def _close_span(self, end: datetime) -> None:
        if self.span is None:
            return
        self._write_duration(end)
        self.span = None
        self._last_flush = None

    def _flush(self, now: datetime) -> None:
        self._write_duration(min(now, self.span.last_seen + timedelta(
            seconds=self.stale_after_s)))
        self._last_flush = now

    def _write_duration(self, end: datetime) -> None:
        seconds = max(0.0, (end - self.span.started_at).total_seconds())
        db.update_event_duration(self.conn, self.span.event_id, seconds)
