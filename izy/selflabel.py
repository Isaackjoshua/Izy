"""The hourly "were you on task?" prompt.

Phase 1 has no classifier, so this is how the `labels` table starts filling up.
Every answer is a hand-labelled example keyed to a real activity event, which
is exactly what Phase 3 needs to check itself against.

It is also the first thing in the codebase allowed to interrupt, so it obeys
the interruption budget like everything else will: never during a break, never
outside active hours, and never while the hourly slot has already been used.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from . import db
from .models import utcnow

log = logging.getLogger(__name__)


def _parse_hhmm(s: str, fallback: time) -> time:
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


class SelfLabelPrompt:
    """Decides *whether* to ask and *what* to ask about. Asking itself is the
    UI's job — kept apart so the policy is testable with no Qt in the room."""

    def __init__(self, conn, cfg, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.last_asked: datetime | None = None

    def _within_active_hours(self, now_local: datetime) -> bool:
        start = _parse_hhmm(self.cfg.self_label.active_from, time(9, 0))
        end = _parse_hhmm(self.cfg.self_label.active_until, time(22, 0))
        t = now_local.time()
        if start <= end:
            return start <= t <= end
        return t >= start or t <= end  # window wrapping past midnight

    def due(self, phase_is_break: bool) -> bool:
        if not self.cfg.self_label.enabled:
            return False
        if phase_is_break:
            return False
        now = self.clock()
        if not self._within_active_hours(now.astimezone()):
            return False
        if self.last_asked is None:
            # Don't ambush the very first minute of a fresh install; wait a
            # full interval so there is something worth asking about.
            self.last_asked = now
            return False
        elapsed = (now - self.last_asked).total_seconds() / 60.0
        return elapsed >= self.cfg.self_label.every_minutes

    def pick_event(self):
        """The most substantial non-AFK event since the last prompt.

        Asking about the longest span rather than the newest one means the
        single question covers the largest slice of time, so one interruption
        buys the most label.
        """
        since = self.last_asked or (self.clock() - timedelta(
            minutes=self.cfg.self_label.every_minutes))
        from .models import to_iso
        return self.conn.execute(
            "SELECT * FROM activity_events"
            " WHERE ts >= ? AND afk = 0 AND window_title IS NOT NULL"
            " ORDER BY duration_s DESC LIMIT 1",
            (to_iso(since),),
        ).fetchone()

    def record(self, event_id: int, on_task: bool, reason: str | None = None) -> int:
        """Persist the answer. source='user' — this is ground truth, not a guess."""
        self.last_asked = self.clock()
        label_id = db.add_label(self.conn, event_id, "user", on_task,
                                confidence=1.0, reason=reason)
        log.info("self-label: event %d on_task=%s", event_id, on_task)
        return label_id

    def skip(self) -> None:
        """Dismissed without answering. Costs the slot — pushing on would turn
        one ignorable prompt into nagging."""
        self.last_asked = self.clock()
        db.record_intervention(self.conn, "self_label", None, "dismissed",
                               now=self.clock())
