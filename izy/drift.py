"""Drift detection: noticing that you have been off-task for a while.

This is the only thing in Izy that speaks unprompted about your work, so it is
deliberately hard to trigger. Every constraint from SPEC.md Feature 4 applies,
and all of them are enforced in `budget.py` rather than here:

  * off-task for >= 4 continuous minutes before an alert is even considered;
  * at most 3 unsolicited interruptions an hour, ever;
  * 15-minute cooldown after you dismiss one;
  * never during a deep-work streak of >= 20 minutes on-task.

The message says what you declared and what you are doing instead, and nothing
else — "You said: fix the dataloader. YouTube, 11 min." No guilt language, no
encouragement, no exclamation marks. Between 3 alerts and 0, prefer 0.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import from_iso, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriftState:
    off_task_minutes: float
    deep_work_minutes: float
    app: str | None


class DriftDetector:
    def __init__(self, conn, cfg, budget, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.budget = budget
        self.clock = clock
        #: One alert per continuous off-task run. The drift check runs every
        #: tick, so without this a single stretch of drift re-alerted at 1 Hz
        #: and spent the entire hourly ceiling in three seconds — precisely the
        #: behaviour that would get Izy closed for good. Cleared when the run
        #: breaks (back on task, or away).
        self._alerted_this_run = False

    def state(self, session_id: int | None) -> DriftState:
        """Current trailing run of off-task and on-task time in this session.

        Computed from labelled events rather than kept in memory, so a restart
        mid-session does not reset a streak that really happened.
        """
        if session_id is None:
            return DriftState(0.0, 0.0, None)

        # LEFT JOIN, and keep AFK rows explicitly: AFK events are never
        # labelled (being away is not a judgement), so an inner join made them
        # invisible and an hour at lunch could not break an off-task run — you
        # would come back to an alert about drift that had already ended.
        # Other unlabelled events (too short to judge, or awaiting tier 4) stay
        # excluded, so a two-second glance does not break a streak either.
        rows = self.conn.execute(
            "SELECT e.app, e.duration_s, e.afk, l.on_task"
            " FROM activity_events e"
            " LEFT JOIN labels l ON l.event_id = e.id"
            " WHERE e.session_id = ? AND (e.afk = 1 OR l.id IS NOT NULL)"
            " ORDER BY e.id DESC", (session_id,)).fetchall()

        off = on = 0.0
        app = None
        for row in rows:
            if row["afk"]:
                # Being away breaks a streak in both directions without
                # counting as either. Walking away is not drift.
                break
            if row["on_task"]:
                if off:
                    break
                on += (row["duration_s"] or 0) / 60.0
            else:
                if on:
                    break
                off += (row["duration_s"] or 0) / 60.0
                app = app or row["app"]
        return DriftState(off, on, app)

    def check(self, session_id: int | None, intent: str | None):
        """(message, intervention_id) when an alert is warranted, else None.

        Recording the intervention here is what makes the budget real: the
        hourly ceiling and the cooldown both read it back from SQLite.
        """
        if not self.cfg.drift.enabled or not session_id or not intent:
            return None

        state = self.state(session_id)
        if state.off_task_minutes <= 0:
            self._alerted_this_run = False    # the run broke; a new one may alert
            return None
        if not self.budget.drift_qualifies(state.off_task_minutes):
            return None
        if self._alerted_this_run:
            return None                        # already said it; saying it again is nagging

        allowed, reason = self.budget.check(
            "drift", deep_work_minutes=state.deep_work_minutes)
        if not allowed:
            log.debug("drift alert suppressed: %s", reason)
            return None

        message = format_alert(intent, state.app, state.off_task_minutes)
        intervention_id = self.budget.record("drift", message)
        self._alerted_this_run = True
        log.info("drift alert: %s", message)
        return message, intervention_id


def format_alert(intent: str, app: str | None, minutes: float) -> str:
    """`You said: fix the dataloader. YouTube, 11 min.`

    Exactly this shape, and no more than this. Tested, because the temptation
    to add a word of encouragement here is what would eventually get Izy closed.
    """
    where = (app or "elsewhere").strip()
    return f"You said: {intent.strip()}. {where}, {int(minutes)} min."
