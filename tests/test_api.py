"""The control-plane API, exercised end to end.

A real Pipeline runs on a background thread (the "tick"), a real StateBus and
CommandQueue join it to the API, and requests go through FastAPI's TestClient.
This is the shape of the daemon: separate threads, joined only by the queue and
the bus. The Phase 1 gate lives here — state is live, a mutation is visible on
the next tick, and stopping the API leaves the tick running.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from izy import config, db
from izy.api.server import build_app
from izy.ipc import CommandQueue, StateBus
from izy.models import Snapshot
from izy.pipeline import Pipeline


class ClockThread:
    """A wall-ish clock the tick pump advances, so sessions and counters move
    without real time passing."""
    def __init__(self):
        self._t = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self._lock = threading.Lock()
    def __call__(self):
        with self._lock:
            return self._t
    def advance(self, seconds):
        with self._lock:
            self._t += timedelta(seconds=seconds)


class TickPump:
    """Runs pipeline.tick() in a loop on its own thread — the daemon's tick."""
    def __init__(self, pipeline, clock, hz=200):
        self.pipeline = pipeline
        self.clock = clock
        self._stop = threading.Event()
        self._interval = 1.0 / hz
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.ticks = 0
    def _run(self):
        while not self._stop.is_set():
            self.pipeline.tick()
            self.ticks += 1
            self.clock.advance(1)
            time.sleep(self._interval)
    def start(self):
        self.thread.start()
    def stop(self):
        self._stop.set()
        self.thread.join(2)


class WatcherStub:
    name = "stub"
    def __init__(self, clock, app="code", title="main.py"):
        self.clock, self.app, self.title = clock, app, title
    def available(self): return True
    def poll(self): return Snapshot(ts=self.clock(), app=self.app, title=self.title)
    def close(self): pass
    def describe(self): return "stub(scripted)"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db_path = tmp_path / "data.db"
    conn = db.connect(db_path)          # create the schema on the writer conn
    cfg = config.Config()
    clock = ClockThread()
    q, bus = CommandQueue(), StateBus()
    pipeline = Pipeline(cfg, conn=conn, clock=clock, watcher=WatcherStub(clock),
                        command_queue=q, state_bus=bus)
    pipeline.start()
    pump = TickPump(pipeline, clock)
    pump.start()
    app = build_app(q, bus, cfg, db_path)
    client = TestClient(app)
    # Wait for the first published snapshot so /state is live.
    for _ in range(200):
        if bus.latest() is not None:
            break
        time.sleep(0.01)
    yield client, pipeline, pump, bus
    pump.stop()
    client.close()
    conn.close()


# --- reads (Phase 1 gate: /state returns live data) ------------------------

def test_state_returns_live_data(harness):
    client, *_ = harness
    r = client.get("/state")
    assert r.status_code == 200
    body = r.json()
    assert body["mascot"] == "asleep"
    assert body["watcher"] == "stub(scripted)"
    assert body["tick"] >= 1


def test_state_tick_advances(harness):
    client, *_ = harness
    t1 = client.get("/state").json()["tick"]
    time.sleep(0.1)
    t2 = client.get("/state").json()["tick"]
    assert t2 > t1, "the tick keeps beating and /state reflects it"


def test_root_health(harness):
    client, *_ = harness
    assert client.get("/").json()["ok"] is True


# --- mutations (Phase 1 gate: a change is visible within a tick) -----------

def test_starting_a_session_over_the_api_shows_up_in_state(harness):
    client, pipeline, *_ = harness
    r = client.post("/sessions", json={"intent": "fix the dataloader", "minutes": 25})
    assert r.status_code == 200 and r.json()["ok"] is True

    body = _wait_state(client, lambda b: b["phase"] == "focus")
    assert body["phase"] == "focus"
    assert body["mascot"] == "neutral"
    assert body["session"]["intent"] == "fix the dataloader"


def _wait_state(client, predicate, tries=50):
    """Poll /state until predicate holds — a mutation applies on the tick that
    drains its command, and the snapshot publishes later in that same tick, so
    there is a sub-tick window where /state still shows the previous value."""
    for _ in range(tries):
        body = client.get("/state").json()
        if predicate(body):
            return body
        time.sleep(0.01)
    return client.get("/state").json()


def test_stopping_a_session_over_the_api(harness):
    client, *_ = harness
    client.post("/sessions", json={"intent": "x", "minutes": 25})
    assert _wait_state(client, lambda b: b["phase"] == "focus")["phase"] == "focus"
    client.post("/sessions/current/stop", json={"outcome": "finished"})
    assert _wait_state(client, lambda b: b["session"] is None)["session"] is None


def test_outcome_applies_to_the_latest_session(harness):
    client, pipeline, *_ = harness
    client.post("/sessions", json={"intent": "x", "minutes": 25})
    client.post("/sessions/current/stop", json={})
    client.post("/sessions/current/outcome", json={"outcome": "partly"})
    time.sleep(0.1)
    row = pipeline.conn.execute(
        "SELECT outcome FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["outcome"] == "partly"


def test_a_reminder_can_be_added_and_listed(harness):
    client, *_ = harness
    r = client.post("/reminders", json={"text": "remind me in 20 minutes to stretch"})
    assert r.status_code == 200
    time.sleep(0.1)
    reminders = client.get("/reminders").json()
    assert any(x["text"] == "stretch" for x in reminders)


def test_empty_intent_is_rejected_by_validation(harness):
    client, *_ = harness
    assert client.post("/sessions", json={"intent": ""}).status_code == 422


# --- report + doctor -------------------------------------------------------

def test_report_endpoint_returns_the_day(harness):
    client, pipeline, *_ = harness
    r = client.get("/report")
    assert r.status_code == 200
    assert "on_task_share" in r.json() and "totals" in r.json()


def test_doctor_reports_schema_and_budget(harness):
    client, *_ = harness
    d = client.get("/doctor").json()
    assert d["schema_version"] == db.SCHEMA_VERSION
    assert d["tier3_budget_per_day"] == config.Config().llm.max_calls_per_day
    assert d["connected"] is True


# --- future phases are named, not faked ------------------------------------

@pytest.mark.parametrize("path", ["/pomodoro/config", "/messages"])
def test_unbuilt_endpoints_return_501_naming_their_phase(harness, path):
    client, *_ = harness
    r = client.get(path)
    assert r.status_code == 501
    assert "Phase" in r.json()["detail"]


# --- events stream ---------------------------------------------------------

def test_events_stream_pushes_state_deltas(harness):
    client, *_ = harness
    with client.websocket_connect("/events") as ws:
        first = ws.receive_json()
        assert "mascot" in first and "tick" in first
        client.post("/sessions", json={"intent": "watch me", "minutes": 25})
        # Drain a few frames until the session appears.
        seen_focus = False
        for _ in range(50):
            frame = ws.receive_json()
            if frame["phase"] == "focus" and frame["session"]:
                seen_focus = True
                break
        assert seen_focus, "starting a session should arrive on /events"


# --- the invariant: killing the API does not stop tracking -----------------

def test_stopping_the_api_leaves_the_tick_running(harness):
    client, pipeline, pump, bus = harness
    before = bus.latest().tick
    client.close()                      # tear down the API client
    time.sleep(0.15)
    after = bus.latest().tick
    assert after > before, "the tick must keep beating with no API attached"
    assert pump.thread.is_alive()


# --- Phase 3: tasks over the API -------------------------------------------

def test_tasks_crud_over_the_api(harness):
    client, *_ = harness
    r = client.post("/tasks", json={"title": "build the widget", "important": True})
    assert r.status_code == 200
    tid = r.json()["task"]["id"]
    assert r.json()["task"]["quadrant"] == "Q2"

    assert any(t["id"] == tid for t in client.get("/tasks").json())

    client.patch(f"/tasks/{tid}", json={"urgent": True})
    assert client.get(f"/tasks/{tid}").json()["quadrant"] == "Q1"

    client.post(f"/tasks/{tid}/quadrant", json={"quadrant": "Q4"})
    assert client.get(f"/tasks/{tid}").json()["quadrant"] == "Q4"

    client.delete(f"/tasks/{tid}")
    assert client.get(f"/tasks/{tid}").status_code == 404


def test_accepting_a_hint_over_the_api(harness):
    client, *_ = harness
    tid = client.post("/tasks", json={"title": "x"}).json()["task"]["id"]
    client.post(f"/tasks/{tid}/hints", json={"app": "zed"})
    assert "zed" in client.get(f"/tasks/{tid}").json()["hints"]["apps"]


def test_a_session_can_be_started_from_a_task(harness):
    client, pipeline, *_ = harness
    tid = client.post("/tasks", json={"title": "build it",
                                      "hints": {"apps": ["obsidian"]}}).json()["task"]["id"]
    client.post("/sessions", json={"intent": "build it", "minutes": 25, "task_id": tid})
    _wait_state(client, lambda b: b["phase"] == "focus")
    # the daemon loaded the task's hints for this session
    time.sleep(0.1)
    assert pipeline.classifier._session_hints == {"apps": ["obsidian"]}
    row = pipeline.conn.execute(
        "SELECT task_id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    assert row["task_id"] == tid
