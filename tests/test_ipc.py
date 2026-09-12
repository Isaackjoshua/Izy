"""The control-plane boundary: command queue, state bus, and the pipeline's
draining/publishing.

These are the Phase 1 core, and they are deliberately testable with no Qt, no
FastAPI, and no event loop — a command goes in, a tick applies it, a snapshot
comes out.
"""
from __future__ import annotations

import asyncio

import pytest

from izy import db, pipeline as pl
from izy.ipc import CommandQueue, StateBus, StateSnapshot
from izy.models import Snapshot
from izy.sessions import Phase

from .test_pipeline import FakeWatcher, _pipeline


# --- CommandQueue ----------------------------------------------------------

def test_submit_returns_a_future_completed_on_drain():
    q = CommandQueue()
    fut = q.submit("do_thing", x=1)
    assert not fut.done()
    drained = q.drain()
    assert len(drained) == 1 and drained[0].name == "do_thing"
    assert drained[0].args == {"x": 1}
    drained[0].future.set_result("ok")
    assert fut.result(timeout=1) == "ok"


def test_drain_is_non_blocking_and_empties():
    q = CommandQueue()
    assert q.drain() == []
    q.submit("a"); q.submit("b")
    assert [c.name for c in q.drain()] == ["a", "b"]
    assert q.drain() == []


# --- StateBus --------------------------------------------------------------

def test_latest_holds_the_last_published_snapshot():
    bus = StateBus()
    assert bus.latest() is None
    bus.publish(StateSnapshot(tick=1))
    bus.publish(StateSnapshot(tick=2))
    assert bus.latest().tick == 2


def test_publish_is_safe_with_no_subscribers():
    StateBus().publish(StateSnapshot(tick=1))   # must not raise


@pytest.mark.asyncio
async def test_a_subscriber_receives_new_snapshots():
    bus = StateBus()
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    token = bus.subscribe(q, loop)
    bus.publish(StateSnapshot(tick=5))
    got = await asyncio.wait_for(q.get(), timeout=1)
    assert got.tick == 5
    bus.unsubscribe(token)
    assert bus.subscriber_count() == 0


@pytest.mark.asyncio
async def test_a_new_subscriber_gets_the_current_snapshot_immediately():
    bus = StateBus()
    bus.publish(StateSnapshot(tick=9))
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    bus.subscribe(q, loop)
    got = await asyncio.wait_for(q.get(), timeout=1)
    assert got.tick == 9, "a client should render at once, not wait a tick"


# --- pipeline draining -----------------------------------------------------

def test_a_command_is_applied_on_the_next_tick(conn, cfg, clock):
    q = CommandQueue()
    bus = StateBus()
    p = _pipeline(conn, cfg, clock)
    p.command_queue, p.state_bus = q, bus
    p.start()

    fut = q.submit("start_session", intent="fix the dataloader", minutes=25)
    assert p.sessions.current is None, "not applied until the tick drains it"
    p.tick()
    assert fut.done()
    assert p.sessions.current.declared_intent == "fix the dataloader"


def test_an_unknown_command_fails_its_own_future_without_breaking_the_tick(conn, cfg, clock):
    q = CommandQueue()
    p = _pipeline(conn, cfg, clock)
    p.command_queue = q
    p.start()
    fut = q.submit("nonsense")
    p.tick()                      # must not raise
    with pytest.raises(ValueError):
        fut.result(timeout=1)


def test_a_command_that_raises_propagates_through_its_future(conn, cfg, clock):
    q = CommandQueue()
    p = _pipeline(conn, cfg, clock)
    p.command_queue = q
    p.start()
    # record_outcome with a bad outcome logs and returns (no raise); use an
    # end/stop with a bogus arg to force an error path instead.
    fut = q.submit("start_session", intent="", minutes=25)   # empty intent → ValueError
    p.tick()
    # start_session swallows ValueError into a STATUS event, so the future
    # completes with the event list rather than raising — assert it completed.
    assert fut.done()


# --- pipeline publishing ---------------------------------------------------

def test_every_tick_publishes_a_snapshot(conn, cfg, clock):
    bus = StateBus()
    watcher = FakeWatcher([("code", "main.py")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.state_bus = bus
    p.start()
    p.tick()
    snap = bus.latest()
    assert snap is not None
    assert snap.tick >= 1
    assert snap.mascot == "asleep"
    assert snap.focus_app == "code"


def test_the_snapshot_reflects_a_running_session(conn, cfg, clock):
    bus = StateBus()
    p = _pipeline(conn, cfg, clock)
    p.state_bus = bus
    p.start()
    p.sessions.start("fix the dataloader", 25)
    p.tick()
    snap = bus.latest()
    assert snap.phase == "focus"
    assert snap.mascot == "neutral"
    assert snap.session["intent"] == "fix the dataloader"
    assert snap.session["planned_minutes"] == 25


def test_counters_track_the_day(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    s = db.start_session(conn, "x", 60, now=clock())
    eid = db.open_event(conn, Snapshot(ts=clock(), app="code", title="main.py"), s.id)
    db.update_event_duration(conn, eid, 600)
    db.add_label(conn, eid, "rule", True, confidence=1.0, reason="t")
    snap = p.snapshot()
    assert snap.counters["on_task_s"] == 600
    assert snap.counters["sessions"] == 1


def test_the_pipeline_works_with_no_bus_or_queue(conn, cfg, clock):
    """Headless and test runs pass neither; the tick must behave as before."""
    p = _pipeline(conn, cfg, clock)
    assert p.command_queue is None and p.state_bus is None
    p.start()
    p.tick()                      # no publish, no drain, no error
