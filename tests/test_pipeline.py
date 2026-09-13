"""The daemon's tick loop, with no Qt in the room.

This file is the reason `pipeline.py` was split out of `worker.py`: the whole
running behaviour of Izy — poll, record, classify, judge drift, fire reminders —
used to live inside a QObject on a worker thread, where exercising it needed an
event loop and a running thread. Here a day of it replays in milliseconds.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from izy import db, pipeline as pl
from izy.models import Snapshot
from izy.reminders import parse as parse_reminder
from izy.reminders import store as reminder_store
from izy.sessions import Phase


class FakeWatcher:
    """Replays a scripted sequence of (app, title), then keeps returning the last.

    The timestamp is stamped at poll time from the injected clock, not baked in
    when the script is written — otherwise every span carries the same instant
    and has zero duration, which silently makes classification tests vacuous.
    """

    name = "fake"

    def __init__(self, script=(), clock=None):
        self.script = list(script)
        self.clock = clock
        self.closed = False

    def available(self):
        return True

    def poll(self):
        if not self.script:
            return None
        app, title = (self.script.pop(0) if len(self.script) > 1
                      else self.script[0])
        return Snapshot(ts=self.clock(), app=app, title=title)

    def close(self):
        self.closed = True

    def describe(self):
        return "fake(scripted)"


def _pipeline(conn, cfg, clock, watcher=None, llm=None, **watcher_cfg):
    if watcher_cfg:
        cfg = replace(cfg, watcher=replace(cfg.watcher, **watcher_cfg))
    if watcher is not None:
        watcher.clock = clock
    return pl.Pipeline(cfg, conn=conn, clock=clock,
                       watcher=watcher or FakeWatcher(clock=clock), llm=llm)


def _kinds(events):
    return [e.kind for e in events]


def _payload(events, kind):
    return next(e.payload for e in events if e.kind == kind)


# --- lifecycle -------------------------------------------------------------

def test_start_reports_the_initial_phase(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    events = p.start()
    assert _payload(events, pl.PHASE)[0] == Phase.IDLE.value


def test_start_resumes_a_session_left_open_by_a_restart(conn, cfg, clock):
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    clock.advance(minutes=5)
    p = _pipeline(conn, cfg, clock)
    events = p.start()
    assert _payload(events, pl.PHASE)[0] == Phase.FOCUS.value
    assert p.sessions.current.id == s.id


def test_stop_closes_everything(conn, cfg, clock):
    watcher = FakeWatcher([("Code", "main.py")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.start()
    p.tick()
    p.stop()
    assert watcher.closed is True


# --- the tick --------------------------------------------------------------

def test_a_tick_records_activity(conn, cfg, clock):
    watcher = FakeWatcher([("Code", "main.py")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.start()
    for _ in range(5):
        p.tick()
        clock.advance(seconds=1)
    p.tracker.flush()
    assert conn.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 1


def test_a_dead_watcher_does_not_take_the_tick_down(conn, cfg, clock):
    class Exploding(FakeWatcher):
        def poll(self):
            raise RuntimeError("d-bus went away")

    p = _pipeline(conn, cfg, clock, Exploding())
    p.start()
    assert p.tick() == [], "a failing poll is survivable, not fatal"


def test_session_started_elsewhere_is_adopted_mid_tick(conn, cfg, clock):
    """`izy start` writes straight to SQLite from another process."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    db.start_session(conn, "fix the dataloader", 25, now=clock())
    clock.advance(seconds=p.RESYNC_EVERY_S + 1)

    events = p.tick()
    assert _payload(events, pl.PHASE)[0] == Phase.FOCUS.value


# --- classification through the pipeline -----------------------------------

def test_closed_spans_are_classified_and_the_open_one_is_left_alone(conn, cfg, clock):
    """The open span's duration is still growing; judging it early would both
    misreport its length and spend its one cached verdict."""
    watcher = FakeWatcher([("Code", "main.py"), ("Spotify", "a song")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.start()
    p.sessions.start("fix the dataloader", 60)

    p.tick()                       # opens the Code span
    clock.advance(seconds=120)
    p.tick()                       # closes Code, opens Spotify
    clock.advance(seconds=120)
    p.tick()

    labels = conn.execute(
        "SELECT e.app, l.on_task FROM labels l"
        " JOIN activity_events e ON e.id = l.event_id").fetchall()
    assert [(r["app"], bool(r["on_task"])) for r in labels] == [("Code", True)]


def test_ending_a_session_settles_the_buffer_first(conn, cfg, clock):
    """After the session ends there is nothing left to judge those windows
    against, so anything buffered has to be flushed before the intent goes."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 60)
    row_id = db.open_event(conn, Snapshot(ts=clock(), app="Obsidian",
                                          title="notes"), p.sessions.current.id)
    db.update_event_duration(conn, row_id, 300)
    row = conn.execute("SELECT * FROM activity_events WHERE id=?", (row_id,)).fetchone()
    p.classifier.consider(row, "fix the dataloader")
    assert p.classifier._pending, "queued for a paid verdict"

    p.end_session("finished")
    assert not p.classifier._pending, "the buffer must be settled, not abandoned"


def test_tier_4_questions_reach_the_ui(conn, cfg, clock, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 60)
    eid = db.open_event(conn, Snapshot(ts=clock(), app="Obsidian", title="notes"),
                        p.sessions.current.id)
    db.update_event_duration(conn, eid, 300)
    row = conn.execute("SELECT * FROM activity_events WHERE id=?", (eid,)).fetchone()
    p.classifier.consider(row, "fix the dataloader")

    # Tier-4 asks are submitted to the arbiter; the emit happens at the dispatch
    # tick step, so it surfaces on the next tick, not the flush call itself.
    p._flush_classifier(force=True)
    events = p.tick()
    assert pl.ASK_ON_TASK in _kinds(events)
    assert _payload(events, pl.ASK_ON_TASK)[0] == eid


# --- reminders through the pipeline ----------------------------------------

def test_a_due_reminder_is_emitted_and_marked_fired(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    reminder_store.add(conn, parse_reminder(
        "remind me in 1 minute to stretch", now=clock()), now=clock())

    assert pl.REMINDER not in _kinds(p.tick())
    clock.advance(minutes=2)
    events = p.tick()
    assert _payload(events, pl.REMINDER)[1] == "stretch"
    assert conn.execute("SELECT status FROM reminders").fetchone()[0] == "fired"


def test_ending_a_session_releases_a_held_reminder(conn, cfg, clock):
    """It waited rather than breaking the focus it exists to protect."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 60)
    reminder_store.add(conn, parse_reminder(
        "remind me in 1 minute to stretch", now=clock()), now=clock())

    clock.advance(minutes=2)
    assert pl.REMINDER not in _kinds(p.tick()), "quiet during a focus session"

    # Ending the session submits the held reminder to the arbiter at the
    # boundary; the arbiter shows it on the next dispatch (tick step 11).
    p.end_session("finished")
    assert pl.REMINDER in _kinds(p.tick())


def test_on_break_reminders_fire_at_the_boundary(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.sessions.start("fix the dataloader", 60)
    reminder_store.add(conn, parse_reminder(
        "remind me on my next break to refill water"), now=clock())

    p.end_session("finished")
    assert _payload(p.tick(), pl.REMINDER)[1] == "refill water"


def test_an_unreadable_reminder_asks_rather_than_guessing(conn, cfg, clock,
                                                          monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    p = _pipeline(conn, cfg, clock)
    p.start()
    events = p.add_reminder("remind me to do the thing")
    assert pl.CONFIRM_REMINDER in _kinds(events)
    assert conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


def test_a_readable_reminder_is_stored_and_confirmed(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    events = p.add_reminder("remind me in 20 minutes to check the training run")
    assert pl.STATUS in _kinds(events)
    assert conn.execute("SELECT raw_text FROM reminders").fetchone()[0] == \
        "check the training run"


def test_app_opened_triggers_fire_once(conn, cfg, clock):
    watcher = FakeWatcher([("Slack", "general")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.start()
    reminder_store.add(conn, parse_reminder(
        "remind me next time I open slack to reply to the PR thread"), now=clock())

    assert pl.REMINDER in _kinds(p.tick())
    clock.advance(seconds=1)
    assert pl.REMINDER not in _kinds(p.tick()), "already fired"


# --- drift through the pipeline --------------------------------------------

def test_drift_reaches_the_ui_once_per_run(conn, cfg, clock):
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 120)
    eid = db.open_event(conn, Snapshot(ts=clock(), app="YouTube", title="a video"),
                        s.id)
    db.update_event_duration(conn, eid, 11 * 60)
    db.add_label(conn, eid, "rule", False, confidence=1.0, reason="app rule")

    assert pl.DRIFT in _kinds(p.tick())
    for _ in range(10):
        clock.advance(seconds=1)
        assert pl.DRIFT not in _kinds(p.tick()), "one alert per run, not per tick"


def test_drift_is_shown_through_the_arbiter_and_logged(conn, cfg, clock):
    """Phase 2: drift no longer records an intervention with a dismiss cooldown.
    It goes through the arbiter, which logs the SHOW to interrupt_log, and any
    response frees the one-at-a-time slot."""
    p = _pipeline(conn, cfg, clock)
    p.start()
    s = p.sessions.start("fix the dataloader", 120)
    eid = db.open_event(conn, Snapshot(ts=clock(), app="YouTube", title="a video"),
                        s.id)
    db.update_event_duration(conn, eid, 11 * 60)
    db.add_label(conn, eid, "rule", False, confidence=1.0, reason="app rule")

    _id, message = _payload(p.tick(), pl.DRIFT)
    assert "YouTube" in message
    shown = conn.execute(
        "SELECT COUNT(*) FROM interrupt_log WHERE kind='drift' AND verdict='show'"
    ).fetchone()[0]
    assert shown == 1, "the show is logged to interrupt_log"

    assert p.arbiter.active is not None, "one interrupt is on screen"
    p.record_drift_response(_id, "dismissed")
    assert p.arbiter.active is None, "the response freed the slot"


# --- the worker's mapping stays in step ------------------------------------

def test_every_pipeline_event_has_a_signal():
    """A new event kind with no signal would be silently dropped at runtime."""
    pytest.importorskip("PySide6.QtCore")
    from izy.worker import TrackerWorker

    kinds = {getattr(pl, name) for name in dir(pl)
             if name.isupper() and isinstance(getattr(pl, name), str)
             and name not in ("PHASE_CHANGED",)}
    mapped = set(TrackerWorker._SIGNALS)
    assert kinds <= mapped, f"unmapped pipeline events: {kinds - mapped}"
    for signal in TrackerWorker._SIGNALS.values():
        assert hasattr(TrackerWorker, signal), f"missing signal: {signal}"


# --- the self-label spam regression ----------------------------------------

def test_the_self_label_prompt_fires_once_not_every_tick(conn, cfg, clock):
    """The bug a user hit: 15 self-label prompts in 18 seconds. The emit path
    never marked the slot used, so due() stayed true and it re-fired on every
    1 Hz tick until dismissed. One ask per interval, and no more."""
    from dataclasses import replace
    cfg = replace(cfg, self_label=replace(cfg.self_label, every_minutes=60,
                                          active_from="00:00", active_until="23:59"))
    watcher = FakeWatcher([("Code", "main.py")])
    p = _pipeline(conn, cfg, clock, watcher)
    p.start()

    # An event worth asking about, and a full interval elapsed since the arm.
    for _ in range(3):
        p.tick(); clock.advance(seconds=1)
    p.tracker.flush()
    clock.advance(minutes=61)

    asks = 0
    for _ in range(30):                    # 30 ticks = 30 seconds of the old bug
        asks += _kinds(p.tick()).count(pl.SELF_LABEL)
        clock.advance(seconds=1)
    assert asks == 1, f"expected exactly one self-label prompt, got {asks}"


def test_the_next_self_label_waits_a_full_interval(conn, cfg, clock):
    from dataclasses import replace
    from izy.models import Snapshot
    cfg = replace(cfg, self_label=replace(cfg.self_label, every_minutes=60,
                                          active_from="00:00", active_until="23:59"))
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.tick()          # arms last_asked (it begins None; the first due() sets it)

    def some_activity(app):
        # A closed span in each interval, so pick_event always has something.
        eid = db.open_event(conn, Snapshot(ts=clock(), app=app, title=f"{app} w"), None)
        db.update_event_duration(conn, eid, 120)

    some_activity("Code")
    clock.advance(minutes=61)
    assert pl.SELF_LABEL in _kinds(p.tick())          # first ask
    clock.advance(minutes=30)
    some_activity("Firefox")
    assert pl.SELF_LABEL not in _kinds(p.tick())      # too soon
    clock.advance(minutes=31)
    assert pl.SELF_LABEL in _kinds(p.tick())          # next interval


# --- Phase 2 gate: all seven kinds in one tick -----------------------------

def test_seven_interrupts_one_tick_shows_one_logs_six(conn, cfg, clock):
    """izy-v2.md §2 phase gate, end to end through the pipeline and the DB:
    submit all seven interrupt kinds in a single tick, exactly one reaches the
    screen (the highest priority), and interrupt_log records six deferrals with
    reasons."""
    from izy.interrupts import Request
    p = _pipeline(conn, cfg, clock)
    p.start()

    kinds = ["urgent_reminder", "session_overrun", "reminder", "drift",
             "self_label", "pomodoro", "message"]
    for k in kinds:
        p.arbiter.submit(Request(k, payload={"event": pl.STATUS, "message": k},
                                 dedupe_key=k, requested_at=clock()))

    ctx = p._arbiter_context(None)
    shown = p.arbiter.dispatch(ctx)
    assert shown.kind == "urgent_reminder", "the highest priority is shown"

    rows = conn.execute(
        "SELECT kind, verdict, reason FROM interrupt_log ORDER BY id").fetchall()
    shows = [r for r in rows if r["verdict"] == "show"]
    defers = [r for r in rows if r["verdict"] == "defer"]
    assert len(shows) == 1 and shows[0]["kind"] == "urgent_reminder"
    assert len(defers) == 6, "the other six are deferred"
    assert all(r["reason"] for r in defers), "every deferral is explained"
    deferred_kinds = {r["kind"] for r in defers}
    assert deferred_kinds == set(kinds) - {"urgent_reminder"}


def test_interrupt_log_survives_and_caps_read_it(conn, cfg, clock):
    """A restart must not hand out a fresh allowance: the cap reads interrupt_log
    from the DB, so a self-label already shown this hour blocks the next."""
    from izy.interrupts import Request
    p = _pipeline(conn, cfg, clock)
    p.start()
    p.arbiter.submit(Request("self_label", payload={"event": pl.STATUS, "message": "a"},
                             dedupe_key="a", requested_at=clock()))
    assert p.arbiter.dispatch(p._arbiter_context(None)).kind == "self_label"
    p.arbiter.acknowledge()

    # A fresh arbiter on the same DB (as after a restart) sees the shown row.
    from izy.interrupts import Arbiter
    fresh = Arbiter(cfg,
                    shown_since=lambda kind, since: db.interrupts_shown_since(conn, kind, since),
                    last_shown=lambda: db.last_interrupt_shown(conn))
    clock.advance(minutes=5)
    fresh.submit(Request("self_label", dedupe_key="b", requested_at=clock()))
    from izy.interrupts import Context
    assert fresh.dispatch(Context(now=clock())) is None, "1/h cap read from the DB"


# --- Phase 3: task-backed sessions load hints ------------------------------

def test_a_task_backed_session_classifies_hinted_apps_for_free(conn, cfg, clock):
    """izy-v2.md §3 phase gate, end to end through the pipeline: start a session
    from a task, and its hinted app is judged on-task at tier 1 with no paid
    call — the tier-3 counter stays put."""
    from izy import tasks as T
    from tests.test_classifier import _client
    from izy.llm import LLM
    client = _client()
    llm = LLM(conn, cfg, client=client, clock=clock)
    # a second window closes the Obsidian span so it is eligible for classifying
    watcher = FakeWatcher([("Obsidian", "widget notes"), ("Zed", "editor")])
    p = _pipeline(conn, cfg, clock, watcher, llm=llm)
    p.start()

    task = T.create(conn, "build the widget", hints={"apps": ["obsidian"]}, now=clock())
    p.start_session("build the widget", 60, task_id=task.id)

    p.tick(); clock.advance(seconds=120)   # opens the Obsidian span
    p.tick(); clock.advance(seconds=5)      # closes Obsidian, opens Zed
    p.tick()
    p._flush_classifier(force=True)

    label = conn.execute(
        "SELECT source, on_task, reason FROM labels l JOIN activity_events e"
        " ON e.id = l.event_id WHERE e.app = 'Obsidian' ORDER BY l.id DESC LIMIT 1"
    ).fetchone()
    assert label is not None and label["on_task"] == 1
    assert "task hint" in label["reason"]
    assert client.calls == [], "hinted app must not reach tier 3"


def test_hints_clear_when_the_task_session_ends(conn, cfg, clock):
    from izy import tasks as T
    from tests.test_classifier import _client
    from izy.llm import LLM
    p = _pipeline(conn, cfg, clock, llm=LLM(conn, cfg, client=_client(), clock=clock))
    p.start()
    task = T.create(conn, "x", hints={"apps": ["obsidian"]}, now=clock())
    p.start_session("x", 60, task_id=task.id)
    assert p.classifier._session_hints == {"apps": ["obsidian"]}
    p.end_session("finished")
    assert p.classifier._session_hints is None, "hints clear at session end"


def test_q2_nudge_goes_through_the_arbiter_once_a_week(conn, cfg, clock):
    from izy import tasks as T
    p = _pipeline(conn, cfg, clock)
    p.start()
    # a neglected Q2 task
    T.create(conn, "learn rust", important=True, now=clock())
    clock.advance(minutes=1)

    events = p.tick()
    statuses = [e.payload[0] for e in events if e.kind == pl.STATUS]
    assert any("Important, not urgent" in m for m in statuses), \
        "the neglected Q2 task is surfaced"
    # a second scan within the hour does not re-nudge
    clock.advance(minutes=30)
    assert not any(e.kind == pl.STATUS for e in p.tick())
