"""Focus sessions and breaks.

Sessions are the reference point for everything the classifier will later do:
not "is this productive in the abstract" but "is this plausibly related to what
I said I was working on". So the declared intent is required, and a session
without one cannot be started.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from enum import Enum

from . import db
from .models import OUTCOMES, Session, utcnow

log = logging.getLogger(__name__)


class Phase(Enum):
    IDLE = "idle"        # no session; mascot asleep, classification off
    FOCUS = "focus"      # session running
    BREAK = "break"      # between sessions; mascot silent except due reminders


class SessionManager:
    def __init__(self, conn, cfg, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self.current: Session | None = None
        self.break_until = None
        self._on_change = []

    def on_change(self, fn) -> None:
        """Register a callback(phase, session) — used to keep the tracker's
        span boundaries and the mascot's visual state in step."""
        self._on_change.append(fn)

    # --- state -------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        if self.current and self.current.is_open:
            return Phase.FOCUS
        if self.break_until and self.clock() < self.break_until:
            return Phase.BREAK
        return Phase.IDLE

    def remaining(self) -> timedelta | None:
        """Time left in the current session or break, or None when idle."""
        if self.phase is Phase.FOCUS:
            end = self.current.started_at + timedelta(minutes=self.current.planned_minutes)
            return max(timedelta(0), end - self.clock())
        if self.phase is Phase.BREAK:
            return max(timedelta(0), self.break_until - self.clock())
        return None

    def is_overrun(self) -> bool:
        return self.phase is Phase.FOCUS and self.remaining() == timedelta(0)

    # --- transitions -------------------------------------------------------

    def start(self, intent: str, minutes: int | None = None) -> Session:
        intent = (intent or "").strip()
        if not intent:
            raise ValueError("a session needs a declared intent")
        if self.current and self.current.is_open:
            self.end()
        minutes = minutes or self.cfg.session.default_minutes
        self.current = db.start_session(self.conn, intent, minutes, now=self.clock())
        self.break_until = None
        log.info("session %d started: %r (%d min)", self.current.id, intent, minutes)
        self._notify()
        return self.current

    def end(self, outcome: str | None = None, *, start_break: bool = True) -> Session | None:
        if not (self.current and self.current.is_open):
            return None
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        now = self.clock()
        db.end_session(self.conn, self.current.id, outcome, now=now)
        self.current.ended_at = now
        self.current.outcome = outcome
        ended = self.current
        self.current = None
        if start_break:
            self.break_until = now + timedelta(minutes=self.cfg.session.break_minutes)
        log.info("session %d ended (outcome=%s)", ended.id, outcome)
        self._notify()
        return ended

    def record_outcome(self, session_id: int, outcome: str) -> None:
        """Answer the 'did you finish?' prompt after the fact — the session may
        already be closed by the time the answer arrives."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
        db.set_outcome(self.conn, session_id, outcome)

    def end_break(self) -> None:
        self.break_until = None
        self._notify()

    # --- startup recovery --------------------------------------------------

    def recover(self) -> Session | None:
        """Reattach to a session left open by a crash, reboot or logout.

        A session still inside its planned window is resumed — a reboot mid-work
        should not silently lose the intent. One left open far past its planned
        end is closed with no outcome rather than credited with the downtime.
        """
        open_ = db.open_session(self.conn)
        if not open_:
            return None
        now = self.clock()
        grace = timedelta(minutes=open_.planned_minutes +
                          self.cfg.session.auto_close_after_minutes)
        if now - open_.started_at > grace:
            db.end_session(self.conn, open_.id, None, now=open_.started_at + grace)
            log.info("closed stale session %d left open across a restart", open_.id)
            self._notify()
            return None
        self.current = open_
        log.info("resumed session %d: %r", open_.id, open_.declared_intent)
        self._notify()
        return open_

    def resync(self) -> bool:
        """Adopt session changes made by another process.

        `izy start` and `izy stop` write straight to SQLite, so without this the
        daemon keeps logging against whatever it last knew and a CLI-started
        session's events land with session_id NULL — unattributable, and
        therefore worthless to the classifier that exists only to compare
        activity against a declared intent.

        Returns True if the phase changed.
        """
        row = db.open_session(self.conn)
        mine = self.current.id if self.current and self.current.is_open else None
        theirs = row.id if row else None
        if mine == theirs:
            return False
        self.current = row
        if row is not None:
            self.break_until = None
        log.info("session state resynced from db: %s -> %s", mine, theirs)
        self._notify()
        return True

    def _notify(self) -> None:
        for fn in self._on_change:
            try:
                fn(self.phase, self.current)
            except Exception:
                log.exception("session change callback failed")
