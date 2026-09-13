"""The interrupt arbiter — the one place anything reaches the screen.

izy-v2.md §2: the hourly self-label spam was not a bug in one feature. It was a
bug in there being four independent things allowed to interrupt you with no
shared budget. So every would-be interruption becomes a `Request`, each tick
submits its requests, and `dispatch()` — the single decision point — shows at
most one and logs a verdict for every other.

No Qt, no DB writes of its own: the arbiter is pure policy over an injected
`Context`, and it hands each decision to a `log` callback the pipeline points at
`interrupt_log`. That keeps it unit-testable — the phase gate (seven kinds in
one tick → exactly one shown, six deferred with reasons) is a plain function
call, no event loop and no screen.

The gate order is exactly izy-v2.md §2, and each gate's verdict — SHOW, DEFER
(held, retried) or DROP (gone) — is the spec's, not invented:

  1  one at a time      an unacknowledged interrupt blocks all others   DEFER
  2  global cooldown    >= 90 s between any two shown                    DEFER
  3  quiet hours        everything below 90 dropped                     DROP
  4  deep-work          during a >= 20-min streak, below 90 defers      DEFER
  5  fullscreen         defer everything below 100                      DEFER
  6  AFK                defer, never drop (you weren't there to see it)  DEFER
  7  per-kind caps      drift 3/h, self-label 1/h, messages 2/h         DROP
  8  per-message        Phase 5                                         —

Deferred requests go to a hold queue re-examined every tick, so a transient gate
(cooldown expiring, deep-work ending, AFK return, leaving fullscreen) drains
itself the moment it lifts — which is exactly "held to the next natural
boundary", generalised rather than duplicated.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable

log = logging.getLogger(__name__)

SHOW, DEFER, DROP = "show", "defer", "drop"

#: Priority ladder (izy-v2.md §2). Higher shows first and clears more gates.
PRIORITY = {
    "urgent_reminder": 100,
    "session_overrun": 90,     # the end-of-session outcome prompt
    "reminder": 70,            # a due reminder
    "drift": 50,
    "self_label": 30,          # the hourly ask, and tier-4 classification asks
    "pomodoro": 20,            # phase-transition notice (Phase 4)
    "message": 10,             # the message library (Phase 5)
}

#: A shown-but-unacknowledged interrupt that is never answered would wedge the
#: one-at-a-time gate forever. Auto-clear the slot after this so a lost ack (a
#: popup closed without a signal) cannot silence Izy permanently.
STALE_ACTIVE_S = 300


@dataclass(frozen=True)
class Request:
    kind: str
    payload: dict = field(default_factory=dict)
    dedupe_key: str = ""
    requested_at: datetime | None = None
    expires_at: datetime | None = None

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.kind, 0)

    def with_defaults(self, now: datetime) -> "Request":
        key = self.dedupe_key or f"{self.kind}:{id(self)}"
        return Request(self.kind, self.payload, key,
                       self.requested_at or now, self.expires_at)


@dataclass
class Context:
    """Everything the gates read, assembled by the pipeline once per tick."""
    now: datetime
    phase: str = "idle"
    afk: bool = False
    deep_work_minutes: float = 0.0
    fullscreen: bool = False


class Arbiter:
    def __init__(self, cfg, *, log_fn: Callable | None = None,
                 shown_since: Callable | None = None,
                 last_shown: Callable | None = None) -> None:
        """`log_fn(kind, priority, dedupe_key, requested_at, verdict, reason,
        now)` records a decision. `shown_since(kind, since)` and `last_shown()`
        read the durable show-history (so caps and the cooldown survive a
        restart); both default to in-memory when not supplied, for tests."""
        self.cfg = cfg
        self._log_fn = log_fn
        self._shown_since = shown_since
        self._last_shown = last_shown

        self._pending: list[Request] = []
        self._hold: dict[str, Request] = {}
        self._active: str | None = None
        self._active_since: datetime | None = None
        #: in-memory fallbacks / caches when no durable history is wired
        self._mem_shown: list[tuple[str, datetime]] = []
        self._log_dedup: dict[str, tuple[str, str]] = {}

    # --- submission --------------------------------------------------------

    def submit(self, request: Request) -> None:
        """Register a would-be interrupt for this tick. Cheap and side-effect
        free — the decision happens in dispatch()."""
        self._pending.append(request)

    # --- acknowledgement ---------------------------------------------------

    def acknowledge(self) -> None:
        """Clear the active slot: the user answered the shown interrupt, so the
        next one may come through. One-at-a-time means there is only ever one to
        clear, so this needs no id."""
        self._active = None
        self._active_since = None

    @property
    def active(self) -> str | None:
        return self._active

    def held_count(self) -> int:
        return len(self._hold)

    # --- the single decision point ----------------------------------------

    def dispatch(self, ctx: Context) -> Request | None:
        """Decide what — if anything — reaches the screen this tick.

        Returns the one Request to show, or None. Everything else is logged
        DEFER (kept, retried next tick) or DROP (gone). Never raises into the
        tick; the pipeline emits the returned request's payload.
        """
        self._expire_stale_active(ctx.now)

        candidates = self._collect(ctx.now)
        candidates.sort(key=lambda r: (-r.priority, r.requested_at or ctx.now))

        shown: Request | None = None
        new_hold: dict[str, Request] = {}
        for req in candidates:
            if shown is not None:
                # One already won this tick — everything below it waits.
                self._record(req, DEFER, "another interrupt shown this tick", ctx.now)
                new_hold[req.dedupe_key] = req
                continue
            verdict, reason = self._gate(req, ctx)
            self._record(req, verdict, reason, ctx.now)
            if verdict == SHOW:
                shown = req
                self._active = req.dedupe_key
                self._active_since = ctx.now
                self._note_shown(req.kind, ctx.now)
            elif verdict == DEFER:
                new_hold[req.dedupe_key] = req
            # DROP: not held, not retried

        self._hold = new_hold
        return shown

    # --- the gates, in spec order -----------------------------------------

    def _gate(self, req: Request, ctx: Context) -> tuple[str, str]:
        if req.expires_at is not None and ctx.now >= req.expires_at:
            return DROP, "expired"

        # 1. one at a time
        if self._active is not None and self._active != req.dedupe_key:
            return DEFER, "an interrupt is already on screen"

        # 2. global cooldown
        last = self._last_shown_at()
        if last is not None:
            elapsed = (ctx.now - last).total_seconds()
            if elapsed < self.cfg.interrupts.global_cooldown_s:
                left = self.cfg.interrupts.global_cooldown_s - elapsed
                return DEFER, f"global cooldown ({left:.0f}s left)"

        # 3. quiet hours — drop below 90
        if req.priority < 90 and self._in_quiet_hours(ctx.now):
            return DROP, "quiet hours"

        # 4. deep-work protection — defer below 90
        if req.priority < 90 and \
                ctx.deep_work_minutes >= self.cfg.interruptions.deep_work_protect_minutes:
            return DEFER, f"deep-work streak ({ctx.deep_work_minutes:.0f}m)"

        # 5. fullscreen / presentation — defer below 100
        if req.priority < 100 and ctx.fullscreen:
            return DEFER, "fullscreen or presentation"

        # 6. AFK — defer, never drop
        if ctx.afk:
            return DEFER, "you are away"

        # 7. per-kind hourly cap — drop over the ceiling
        cap = self._cap_for(req.kind)
        if cap is not None:
            shown = self._shown_last_hour(req.kind, ctx.now)
            if shown >= cap:
                return DROP, f"hourly cap for {req.kind} ({shown}/{cap})"

        # 8. per-message cooldown — Phase 5
        return SHOW, "ok"

    # --- gate helpers ------------------------------------------------------

    def _cap_for(self, kind: str) -> int | None:
        c = self.cfg
        return {
            "drift": c.interruptions.max_per_hour,
            "self_label": c.interrupts.self_label_per_hour,
            "message": c.interrupts.message_per_hour,
        }.get(kind)

    def _in_quiet_hours(self, now: datetime) -> bool:
        t = now.astimezone().time()
        for window in self.cfg.interrupts.quiet_hours or ():
            try:
                start = _hhmm(window[0])
                end = _hhmm(window[1])
            except (ValueError, IndexError, TypeError):
                continue
            if start <= end:
                if start <= t <= end:
                    return True
            elif t >= start or t <= end:   # window wraps past midnight
                return True
        return False

    def _last_shown_at(self) -> datetime | None:
        if self._last_shown is not None:
            durable = self._last_shown()
            mem = self._mem_shown[-1][1] if self._mem_shown else None
            return max(x for x in (durable, mem) if x is not None) \
                if (durable or mem) else None
        return self._mem_shown[-1][1] if self._mem_shown else None

    def _shown_last_hour(self, kind: str, now: datetime) -> int:
        since = now - timedelta(hours=1)
        mem = sum(1 for k, ts in self._mem_shown if k == kind and ts >= since)
        durable = self._shown_since(kind, since) if self._shown_since else 0
        return max(mem, durable)

    def _note_shown(self, kind: str, now: datetime) -> None:
        self._mem_shown.append((kind, now))
        cutoff = now - timedelta(hours=2)
        self._mem_shown = [(k, t) for k, t in self._mem_shown if t >= cutoff]

    def _collect(self, now: datetime) -> list[Request]:
        """This tick's submissions plus everything held, deduped by key with the
        highest priority winning. Expired held items are dropped here so they do
        not linger."""
        merged: dict[str, Request] = {}
        for req in list(self._hold.values()) + self._pending:
            req = req.with_defaults(now)
            if req.expires_at is not None and now >= req.expires_at:
                self._record(req, DROP, "expired", now)
                continue
            existing = merged.get(req.dedupe_key)
            if existing is None or req.priority > existing.priority:
                merged[req.dedupe_key] = req
        self._pending = []
        return list(merged.values())

    def _expire_stale_active(self, now: datetime) -> None:
        if self._active is not None and self._active_since is not None:
            if (now - self._active_since).total_seconds() >= STALE_ACTIVE_S:
                log.debug("clearing stale active interrupt %s", self._active)
                self.acknowledge()

    def _record(self, req: Request, verdict: str, reason: str, now: datetime) -> None:
        # A held request is re-evaluated every tick; only log when its verdict or
        # reason changes, so interrupt_log stays a record of decisions rather
        # than a per-second stream. A SHOW is always an event worth logging.
        prev = self._log_dedup.get(req.dedupe_key)
        if verdict != SHOW and prev == (verdict, reason):
            return
        self._log_dedup[req.dedupe_key] = (verdict, reason)
        if verdict == SHOW or verdict == DROP:
            # terminal for this key; forget the dedup so a future request with
            # the same key logs afresh
            self._log_dedup.pop(req.dedupe_key, None) if verdict == DROP else None
        if self._log_fn is not None:
            try:
                self._log_fn(req.kind, req.priority, req.dedupe_key,
                             req.requested_at or now, verdict, reason, now)
            except Exception:
                log.exception("interrupt log failed")
        log.debug("interrupt %s p%d -> %s (%s)", req.kind, req.priority, verdict, reason)


def _hhmm(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))
