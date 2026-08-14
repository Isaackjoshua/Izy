from __future__ import annotations

import pytest

from izy import db
from izy.sessions import Phase, SessionManager


def test_session_requires_an_intent(conn, cfg, clock):
    """The classifier's whole reference point is the declared intent, so a
    session without one is meaningless rather than merely empty."""
    sm = SessionManager(conn, cfg, clock=clock)
    with pytest.raises(ValueError):
        sm.start("   ")


def test_start_end_records_outcome(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    sm.start("fix the dataloader", 25)
    assert sm.phase is Phase.FOCUS
    clock.advance(minutes=25)
    ended = sm.end("partly")
    assert ended.outcome == "partly"

    row = conn.execute("SELECT * FROM sessions WHERE id=?", (ended.id,)).fetchone()
    assert row["outcome"] == "partly" and row["ended_at"] is not None


def test_rejects_an_unknown_outcome(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    sm.start("write tests")
    with pytest.raises(ValueError):
        sm.end("kind of")


def test_break_follows_a_session_then_lapses_to_idle(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    sm.start("write tests")
    sm.end("finished")
    assert sm.phase is Phase.BREAK
    clock.advance(minutes=cfg.session.break_minutes + 1)
    assert sm.phase is Phase.IDLE


def test_starting_again_closes_the_previous_session(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    first = sm.start("thing one")
    clock.advance(minutes=5)
    sm.start("thing two")
    assert conn.execute(
        "SELECT ended_at FROM sessions WHERE id=?", (first.id,)).fetchone()[0] is not None


def test_remaining_counts_down_and_overrun_is_detected(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    sm.start("fix the dataloader", 25)
    clock.advance(minutes=10)
    assert sm.remaining().total_seconds() == pytest.approx(15 * 60)
    assert sm.is_overrun() is False
    clock.advance(minutes=20)
    assert sm.is_overrun() is True


def test_recover_resumes_a_session_interrupted_by_a_reboot(conn, cfg, clock):
    """A reboot mid-work should not silently lose what you said you were doing."""
    started = db.start_session(conn, "fix the dataloader", 25, now=clock())
    clock.advance(minutes=5)

    sm = SessionManager(conn, cfg, clock=clock)
    resumed = sm.recover()
    assert resumed is not None and resumed.id == started.id
    assert sm.phase is Phase.FOCUS


def test_recover_closes_a_session_left_open_overnight(conn, cfg, clock):
    """Downtime must not be credited as focus time."""
    db.start_session(conn, "fix the dataloader", 25, now=clock())
    clock.advance(minutes=60 * 12)

    sm = SessionManager(conn, cfg, clock=clock)
    assert sm.recover() is None
    assert sm.phase is Phase.IDLE
    row = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ended_at"] is not None
    assert row["outcome"] is None, "an unanswered session has no outcome, not a guessed one"


def test_change_callbacks_fire_on_transitions(conn, cfg, clock):
    seen = []
    sm = SessionManager(conn, cfg, clock=clock)
    sm.on_change(lambda phase, session: seen.append(phase))
    sm.start("a thing")
    sm.end("finished")
    assert seen == [Phase.FOCUS, Phase.BREAK]


def test_resync_adopts_a_session_started_by_the_cli(conn, cfg, clock):
    """`izy start` writes straight to SQLite from another process. Without a
    resync the daemon keeps logging session_id NULL for the whole session."""
    sm = SessionManager(conn, cfg, clock=clock)
    assert sm.phase is Phase.IDLE

    external = db.start_session(conn, "fix the dataloader", 25, now=clock())
    assert sm.resync() is True
    assert sm.phase is Phase.FOCUS
    assert sm.current.id == external.id


def test_resync_notices_a_session_stopped_by_the_cli(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    s = sm.start("fix the dataloader")
    db.end_session(conn, s.id, "finished", now=clock())   # `izy stop` elsewhere

    assert sm.resync() is True
    assert sm.phase is Phase.IDLE


def test_resync_is_a_no_op_when_nothing_changed(conn, cfg, clock):
    sm = SessionManager(conn, cfg, clock=clock)
    sm.start("fix the dataloader")
    assert sm.resync() is False
    changes = []
    sm.on_change(lambda p, s: changes.append(p))
    assert sm.resync() is False
    assert changes == [], "a steady state must not churn tracker span boundaries"
