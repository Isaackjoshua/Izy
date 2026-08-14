from __future__ import annotations

from izy import db
from izy.tracker import Tracker

from .conftest import snap


def _events(conn):
    return conn.execute("SELECT * FROM activity_events ORDER BY id").fetchall()


def test_identical_snapshots_coalesce_into_one_span(conn, clock):
    t = Tracker(conn, flush_interval_s=5, clock=clock)
    for _ in range(30):
        t.tick(snap(clock))
        clock.advance(seconds=1)
    t.flush()

    rows = _events(conn)
    assert len(rows) == 1, "30 identical polls must not write 30 rows"
    assert rows[0]["duration_s"] >= 29


def test_title_change_closes_the_span_and_opens_a_new_one(conn, clock):
    t = Tracker(conn, clock=clock)
    t.tick(snap(clock, title="main.py"))
    clock.advance(seconds=10)
    t.tick(snap(clock, title="test_main.py"))
    clock.advance(seconds=5)
    t.flush()

    rows = _events(conn)
    assert [r["window_title"] for r in rows] == ["main.py", "test_main.py"]
    assert rows[0]["duration_s"] == 10


def test_afk_change_alone_splits_the_span(conn, clock):
    t = Tracker(conn, clock=clock)
    t.tick(snap(clock))
    clock.advance(seconds=60)
    t.tick(snap(clock, afk=True))
    t.flush()

    rows = _events(conn)
    assert len(rows) == 2
    assert rows[1]["afk"] == 1


def test_watcher_gap_does_not_invent_time(conn, clock):
    """If the watcher goes dark we must not credit the span with the downtime —
    that is how 'hours focused' numbers quietly become fiction."""
    t = Tracker(conn, stale_after_s=60, clock=clock)
    t.tick(snap(clock))
    clock.advance(seconds=10)
    t.tick(snap(clock))          # last confirmed sighting at t+10

    for _ in range(20):          # 20 minutes of nothing
        clock.advance(minutes=1)
        t.tick(None)

    rows = _events(conn)
    assert len(rows) == 1
    assert rows[0]["duration_s"] == 10, "span credited only up to its last sighting"


def test_span_reopens_after_a_gap(conn, clock):
    t = Tracker(conn, stale_after_s=30, clock=clock)
    t.tick(snap(clock))
    clock.advance(minutes=5)
    t.tick(None)                 # closes as stale
    t.tick(snap(clock))          # same window, but a new span
    clock.advance(seconds=10)
    t.flush()

    assert len(_events(conn)) == 2


def test_session_boundary_splits_the_span(conn, clock):
    t = Tracker(conn, clock=clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    t.set_session(s.id)
    t.tick(snap(clock))
    clock.advance(seconds=30)

    t.set_session(None)          # session ended mid-window
    t.tick(snap(clock))
    clock.advance(seconds=30)
    t.flush()

    rows = _events(conn)
    assert len(rows) == 2
    assert rows[0]["session_id"] == s.id
    assert rows[1]["session_id"] is None


def test_flush_writes_duration_before_a_crash(conn, clock):
    """Whatever the flush interval is, that is the most a hard crash can lose."""
    t = Tracker(conn, flush_interval_s=15, clock=clock)
    t.tick(snap(clock))
    for _ in range(20):
        clock.advance(seconds=1)
        t.tick(snap(clock))
    # No flush()/stop() — simulate the process being killed here.
    assert _events(conn)[0]["duration_s"] >= 15
