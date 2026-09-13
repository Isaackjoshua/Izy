"""The interrupt arbiter — the single dispatch point (izy-v2.md §2).

The headline is the phase gate: fire all seven kinds in one tick and exactly one
shows, in priority order, with six deferrals logged and explained. The rest pin
each gate and the hold queue, because the arbiter is the thing standing between
Izy and being the nag it exists to prevent.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from izy import config
from izy.interrupts import DEFER, DROP, SHOW, Arbiter, Context, Request


def _cfg(**interrupts):
    c = config.Config()
    if interrupts:
        c = replace(c, interrupts=replace(c.interrupts, **interrupts))
    return c


class LogSpy:
    def __init__(self):
        self.rows = []

    def __call__(self, kind, priority, key, requested_at, verdict, reason, now):
        self.rows.append({"kind": kind, "priority": priority, "verdict": verdict,
                          "reason": reason})

    def by_verdict(self, v):
        return [r for r in self.rows if r["verdict"] == v]


def _arbiter(cfg=None, log=None):
    return Arbiter(cfg or _cfg(), log_fn=log)


def _ctx(now=None, **kw):
    return Context(now=now or datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc), **kw)


def _req(kind, key=None, **kw):
    return Request(kind, dedupe_key=key or kind, **kw)


ALL_SEVEN = ["urgent_reminder", "session_overrun", "reminder", "drift",
             "self_label", "pomodoro", "message"]


# --- the phase gate --------------------------------------------------------

def test_seven_kinds_one_tick_shows_exactly_one_and_defers_six():
    log = LogSpy()
    arb = _arbiter(log=log)
    for kind in ALL_SEVEN:
        arb.submit(_req(kind))

    shown = arb.dispatch(_ctx())

    assert shown is not None
    assert shown.kind == "urgent_reminder", "the highest priority wins"
    shows = log.by_verdict(SHOW)
    assert len(shows) == 1 and shows[0]["kind"] == "urgent_reminder"
    deferrals = log.by_verdict(DEFER)
    assert len(deferrals) == 6, "the other six are deferred"
    assert all(r["reason"] for r in deferrals), "every deferral is explained"


def test_priority_order_is_respected_over_several_ticks():
    """With the winner acknowledged each tick, the held ones surface in order."""
    arb = _arbiter()
    for kind in ALL_SEVEN:
        arb.submit(_req(kind))

    seen = []
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    for _ in range(len(ALL_SEVEN)):
        shown = arb.dispatch(_ctx(now=now))
        if shown:
            seen.append(shown.kind)
            arb.acknowledge()
        now += timedelta(seconds=100)          # clear the 90s global cooldown
    assert seen == ALL_SEVEN, "shown strictly high-to-low priority"


# --- gate 1: one at a time -------------------------------------------------

def test_an_unacknowledged_interrupt_blocks_all_others():
    arb = _arbiter()
    arb.submit(_req("drift"))
    first = arb.dispatch(_ctx())
    assert first.kind == "drift"

    arb.submit(_req("urgent_reminder"))
    now = _ctx().now + timedelta(seconds=200)   # past cooldown
    assert arb.dispatch(_ctx(now=now)) is None, "still one on screen"

    arb.acknowledge()
    assert arb.dispatch(_ctx(now=now)).kind == "urgent_reminder"


def test_a_lost_acknowledgement_does_not_wedge_forever():
    from izy.interrupts.arbiter import STALE_ACTIVE_S
    arb = _arbiter()
    arb.submit(_req("drift"))
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert arb.dispatch(_ctx(now=now)).kind == "drift"

    arb.submit(_req("urgent_reminder"))
    later = now + timedelta(seconds=STALE_ACTIVE_S + 1)
    assert arb.dispatch(_ctx(now=later)).kind == "urgent_reminder", \
        "the stale active slot auto-clears"


# --- gate 2: global cooldown ----------------------------------------------

def test_global_cooldown_defers_everything_including_urgent():
    arb = _arbiter(_cfg(global_cooldown_s=90))
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    arb.submit(_req("drift"))
    assert arb.dispatch(_ctx(now=now)).kind == "drift"
    arb.acknowledge()

    arb.submit(_req("urgent_reminder"))
    assert arb.dispatch(_ctx(now=now + timedelta(seconds=30))) is None
    arb.submit(_req("urgent_reminder"))
    assert arb.dispatch(_ctx(now=now + timedelta(seconds=91))).kind == "urgent_reminder"


# --- gate 3: quiet hours ---------------------------------------------------

def test_quiet_hours_drops_below_90():
    cfg = _cfg(quiet_hours=[["22:00", "07:30"]])
    log = LogSpy()
    arb = Arbiter(cfg, log_fn=log)
    night = datetime(2026, 9, 13, 23, 0).astimezone()
    arb.submit(_req("drift"))
    assert arb.dispatch(_ctx(now=night)) is None
    assert any(r["kind"] == "drift" and r["verdict"] == DROP for r in log.rows)
    assert arb.held_count() == 0, "dropped, not held"


def test_quiet_hours_lets_priority_90_through():
    cfg = _cfg(quiet_hours=[["22:00", "07:30"]])
    arb = Arbiter(cfg)
    night = datetime(2026, 9, 13, 23, 0).astimezone()
    arb.submit(_req("session_overrun"))
    assert arb.dispatch(_ctx(now=night)).kind == "session_overrun"


def test_quiet_hours_off_window_shows_normally():
    cfg = _cfg(quiet_hours=[["22:00", "07:30"]])
    arb = Arbiter(cfg)
    noon = datetime(2026, 9, 13, 12, 0).astimezone()
    arb.submit(_req("drift"))
    assert arb.dispatch(_ctx(now=noon)).kind == "drift"


# --- gate 4: deep-work -----------------------------------------------------

def test_deep_work_defers_below_90():
    arb = _arbiter()
    arb.submit(_req("drift"))
    arb.submit(_req("session_overrun"))
    shown = arb.dispatch(_ctx(deep_work_minutes=25))
    assert shown.kind == "session_overrun", "90 clears deep-work; drift defers"
    assert arb.held_count() == 1


# --- gate 5: fullscreen ----------------------------------------------------

def test_fullscreen_defers_below_100():
    arb = _arbiter()
    arb.submit(_req("session_overrun"))       # 90
    arb.submit(_req("urgent_reminder"))       # 100
    shown = arb.dispatch(_ctx(fullscreen=True))
    assert shown.kind == "urgent_reminder", "only 100 clears fullscreen"


# --- gate 6: AFK -----------------------------------------------------------

def test_afk_defers_and_never_drops():
    log = LogSpy()
    arb = Arbiter(_cfg(), log_fn=log)
    arb.submit(_req("urgent_reminder"))
    assert arb.dispatch(_ctx(afk=True)) is None
    assert arb.held_count() == 1, "held, not dropped"
    assert all(r["verdict"] != DROP for r in log.rows)
    # returns the moment you are back
    assert arb.dispatch(_ctx(afk=False)).kind == "urgent_reminder"


# --- gate 7: per-kind caps -------------------------------------------------

def test_self_label_capped_at_one_per_hour():
    arb = _arbiter(_cfg(self_label_per_hour=1, global_cooldown_s=0))
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    arb.submit(_req("self_label", key="sl1"))
    assert arb.dispatch(_ctx(now=now)).kind == "self_label"
    arb.acknowledge()

    arb.submit(_req("self_label", key="sl2"))
    log = LogSpy(); arb._log_fn = log
    assert arb.dispatch(_ctx(now=now + timedelta(minutes=5))) is None
    assert any(r["verdict"] == DROP and "cap" in r["reason"] for r in log.rows)


def test_drift_cap_uses_interruptions_max_per_hour():
    cfg = replace(_cfg(global_cooldown_s=0),
                  interruptions=replace(config.Config().interruptions, max_per_hour=2))
    arb = Arbiter(cfg)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    for i in range(2):
        arb.submit(_req("drift", key=f"d{i}"))
        assert arb.dispatch(_ctx(now=now)).kind == "drift"
        arb.acknowledge()
        now += timedelta(minutes=1)
    arb.submit(_req("drift", key="d3"))
    assert arb.dispatch(_ctx(now=now)) is None, "third drift over the 2/h cap"


# --- durable history (survives a restart) ----------------------------------

def test_caps_and_cooldown_read_durable_history():
    """A restart must not hand out a fresh allowance."""
    shown = {"drift": 3}
    last = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    cfg = replace(_cfg(global_cooldown_s=90),
                  interruptions=replace(config.Config().interruptions, max_per_hour=3))
    arb = Arbiter(cfg,
                  shown_since=lambda kind, since: shown.get(kind, 0),
                  last_shown=lambda: last)
    arb.submit(_req("drift"))
    # both the cap (3/3 already) and the cooldown apply from durable history
    assert arb.dispatch(_ctx(now=last + timedelta(seconds=30))) is None


# --- expiry + dedup --------------------------------------------------------

def test_an_expired_request_is_dropped():
    arb = _arbiter()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    arb.submit(_req("reminder", expires_at=now - timedelta(seconds=1)))
    assert arb.dispatch(_ctx(now=now)) is None


def test_same_key_deduped_to_highest_priority():
    arb = _arbiter()
    arb.submit(Request("self_label", dedupe_key="k"))
    arb.submit(Request("drift", dedupe_key="k"))     # same key, higher priority
    shown = arb.dispatch(_ctx())
    assert shown.kind == "drift"
    assert arb.held_count() == 0, "the duplicate did not also get held"


def test_a_persistent_defer_is_logged_once_not_every_tick():
    log = LogSpy()
    arb = Arbiter(_cfg(), log_fn=log)
    arb.submit(_req("drift"))
    arb.dispatch(_ctx())                # shown
    arb.submit(_req("self_label"))      # will be blocked by one-at-a-time
    for i in range(10):
        arb.dispatch(_ctx())            # held, re-evaluated each tick
    defers = log.by_verdict(DEFER)
    assert len(defers) == 1, "a steady deferral logs once, not per tick"
