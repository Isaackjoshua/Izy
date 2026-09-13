"""The drift threshold.

Until Phase 2 this module was the interruption budget: it decided whether any
unsolicited alert could be shown, reading its history from `interventions`. The
interrupt arbiter (izy-v2.md §2, `izy/interrupts/arbiter.py`) now owns that whole
decision — one place, all kinds, the global cooldown and per-kind caps read from
`interrupt_log`. What survives here is the one pure question the arbiter and the
mascot both still ask: has off-task time crossed the drift threshold?

Kept as a small class rather than a free function only so `drift.state` and
`mascot_state` can keep calling `self.budget.drift_qualifies(...)` unchanged.
"""
from __future__ import annotations

from .models import utcnow


class InterruptionBudget:
    def __init__(self, conn, cfg, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock

    def drift_qualifies(self, off_task_minutes: float) -> bool:
        """Brief context switches are normal work, not failure. The threshold
        lives here so drift detection and the mascot's soft-alert posture agree
        on exactly when 'off task' becomes 'drifting'."""
        return off_task_minutes >= self.cfg.interruptions.drift_min_minutes
