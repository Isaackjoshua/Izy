"""When a reminder actually fires.

The rule that shapes this whole module, from SPEC.md Feature 3:

    Reminders fire even during a focus session, but only at the *next* natural
    boundary (session end or break) unless marked urgent when created. A
    reminder that breaks the focus it's supposed to protect is a bug.

So a due reminder mid-session is *held*, not dropped and not fired. It is
released at the next boundary — or once it has been held past
`max_defer_minutes`, since a reminder that arrives an hour late is worse than
one that arrives slightly early.

Reminders are solicited: you asked for them. They do not consume the
interruption budget, which is reserved for things Izy decides to say on its own.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from ..models import utcnow
from ..sessions import Phase
from . import store

log = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, conn, cfg, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        #: reminder id -> the moment it first came due while held
        self._held: dict[int, object] = {}

    # --- what is ready to show --------------------------------------------

    def due_now(self, phase: Phase) -> list[store.Reminder]:
        """Time reminders that should be shown at this instant."""
        now = self.clock()
        out = []
        for r in store.due_by_time(self.conn, now=now):
            self._held.setdefault(r.id, r.due_at or now)
            if self._may_fire_now(r, phase, now):
                out.append(r)
        return out

    def _may_fire_now(self, reminder, phase: Phase, now) -> bool:
        if phase is not Phase.FOCUS:
            return True                      # idle or on a break: free to speak
        if not self.cfg.reminders.defer_during_focus:
            return True
        if self._is_urgent(reminder):
            return True
        held_since = self._held.get(reminder.id, now)
        overdue = (now - held_since).total_seconds() / 60.0
        if overdue >= self.cfg.reminders.max_defer_minutes:
            log.info("reminder %d held %.0fm past due; releasing mid-session",
                     reminder.id, overdue)
            return True
        return False

    @staticmethod
    def _is_urgent(reminder) -> bool:
        """Urgency is recorded at creation time, in the stored text, because the
        reminders table in SPEC.md has no urgent column and inventing one would
        be a schema change for a single flag."""
        return reminder.raw_text.lower().startswith("[urgent]")

    def on_context(self, context: str) -> list[store.Reminder]:
        """Context-triggered reminders for an event that just happened.

        Boundaries are exactly when it is safe to speak, so these fire
        immediately with no deferral check.
        """
        return store.due_by_context(self.conn, context)

    def on_boundary(self, phase: Phase) -> list[store.Reminder]:
        """Everything held during a session, released now that we are at a
        boundary."""
        now = self.clock()
        return [r for r in store.due_by_time(self.conn, now=now)]

    # --- transitions -------------------------------------------------------

    def contexts_for_phase_change(self, old: Phase, new: Phase) -> list[str]:
        contexts = []
        if new is Phase.FOCUS and old is not Phase.FOCUS:
            contexts.append("session_start")
        if old is Phase.FOCUS and new is not Phase.FOCUS:
            contexts.append("session_end")
        if new is Phase.BREAK:
            contexts.append("on_break")
        return contexts

    def end_of_day_due(self, last_checked) -> bool:
        """True once, when the configured hour is first crossed."""
        now_local = self.clock().astimezone()
        if now_local.hour < self.cfg.reminders.end_of_day_hour:
            return False
        if last_checked is None:
            return True
        return last_checked.astimezone().date() < now_local.date()

    # --- responses ---------------------------------------------------------

    def fired(self, reminder_id: int) -> None:
        store.mark_fired(self.conn, reminder_id, now=self.clock())
        self._held.pop(reminder_id, None)

    def done(self, reminder_id: int) -> None:
        store.mark_done(self.conn, reminder_id)
        self._held.pop(reminder_id, None)

    def dismiss(self, reminder_id: int) -> None:
        store.mark_dismissed(self.conn, reminder_id)
        self._held.pop(reminder_id, None)

    def snooze(self, reminder_id: int):
        self._held.pop(reminder_id, None)
        return store.snooze(self.conn, reminder_id,
                            self.cfg.reminders.snooze_minutes, now=self.clock())
