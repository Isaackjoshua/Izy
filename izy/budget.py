"""The interruption budget, enforced as code rather than as a guideline.

SPEC.md Feature 4 is explicit that this is a hard requirement, so it lives in
one place that every would-be interruption must pass through, and it reads its
history from the `interventions` table rather than from memory — a restart must
not hand Izy a fresh allowance to spend.

The rule that decides ties: between 3 alerts and 0 alerts, prefer 0.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from . import db
from .models import from_iso, utcnow

log = logging.getLogger(__name__)

#: Interruptions the user asked for are not interruptions. Only unsolicited
#: ones are charged to the budget.
UNSOLICITED_KINDS = ("drift", "self_label", "session_overrun")


class InterruptionBudget:
    def __init__(self, conn, cfg, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock

    def check(self, kind: str, *, deep_work_minutes: float = 0.0) -> tuple[bool, str]:
        """May we interrupt right now? Returns (allowed, reason).

        The reason is returned even when allowed, because it is what makes the
        budget debuggable when it feels wrong — you can ask Izy why it stayed
        quiet instead of guessing.
        """
        if kind not in UNSOLICITED_KINDS:
            return True, "solicited"

        c = self.cfg.interruptions
        now = self.clock()

        if deep_work_minutes >= c.deep_work_protect_minutes:
            return False, (f"deep-work streak {deep_work_minutes:.0f}m "
                           f"(>= {c.deep_work_protect_minutes}m)")

        recent = db.interventions_since(self.conn, now - timedelta(hours=1))
        unsolicited = [r for r in recent if r["kind"] in UNSOLICITED_KINDS]
        if len(unsolicited) >= c.max_per_hour:
            return False, f"hourly ceiling reached ({len(unsolicited)}/{c.max_per_hour})"

        cooldown = timedelta(minutes=c.cooldown_after_dismiss_minutes)
        for r in reversed(unsolicited):
            if r["user_response"] == "dismissed":
                since = now - from_iso(r["ts"])
                if since < cooldown:
                    left = (cooldown - since).total_seconds() / 60.0
                    return False, f"cooldown after dismissal ({left:.0f}m left)"
                break

        return True, f"allowed ({len(unsolicited)}/{c.max_per_hour} used this hour)"

    def drift_qualifies(self, off_task_minutes: float) -> bool:
        """Brief context switches are normal work, not failure. Phase 3 calls
        this; it lives here so the threshold has exactly one home."""
        return off_task_minutes >= self.cfg.interruptions.drift_min_minutes

    def record(self, kind: str, message: str | None = None,
               response: str | None = None) -> int:
        return db.record_intervention(self.conn, kind, message, response, now=self.clock())

    def resolve(self, intervention_id: int, response: str) -> None:
        db.set_intervention_response(self.conn, intervention_id, response)
