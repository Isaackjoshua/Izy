"""The FastAPI control-plane app and its Unix-domain-socket runner.

Two things live here:

  * `build_app(command_queue, state_bus, cfg, db_path)` — the ASGI app. It is
    pure of Qt and of the tick: reads come from the `StateBus` and a read-only
    DB connection; every mutation is a `CommandQueue.submit`, whose Future the
    tick completes on the writer thread. This is what keeps "one writer" true
    (izy-v2.md §1). The app is fully testable in-process with httpx's ASGI
    transport — no socket required.

  * `ApiServer` — runs uvicorn on the UDS in its own thread inside the daemon.
    The tick keeps running whether or not this thread is alive, which is the
    Phase 1 gate: killing the API does not stop tracking.

Endpoints that back existing features are live. Endpoints for later phases
(tasks, pomodoro, messages) return 501 naming their phase rather than pretending
to exist — the plan is built one phase at a time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from concurrent.futures import Future
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import db, paths
from ..ipc import CommandQueue, StateBus
from . import schemas

log = logging.getLogger(__name__)

#: How long a mutating request waits for the tick to apply its command before
#: returning anyway. Two ticks: the command is drained at the very top of the
#: next tick, so one tick's latency is the norm and two is the ceiling.
COMMAND_TIMEOUT_S = 2.5

_NOT_YET = {
    "/pomodoro": "Phase 4 (pomodoro)",
    "/messages": "Phase 5 (message library)",
}


def build_app(command_queue: CommandQueue, state_bus: StateBus, cfg,
              db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="izyd", version="2.0", docs_url=None, redoc_url=None)
    db_path = db_path or paths.db_path()

    def _wait(future: Future):
        """Block a request thread on a command Future. uvicorn runs handlers in
        a threadpool, so this does not stall the event loop."""
        try:
            return future.result(timeout=COMMAND_TIMEOUT_S)
        except TimeoutError:
            raise HTTPException(504, "the daemon did not apply the command in time")
        except Exception as e:
            raise HTTPException(400, str(e))

    def _state_out() -> schemas.StateOut | None:
        snap = state_bus.latest()
        return schemas.StateOut(**snap.to_dict()) if snap else None

    def _accepted(detail: str | None = None) -> schemas.Accepted:
        return schemas.Accepted(ok=True, detail=detail, state=_state_out())

    # --- reads -------------------------------------------------------------

    @app.get("/")
    def root():
        snap = state_bus.latest()
        return {"ok": True, "tick": snap.tick if snap else 0}

    @app.get("/state", response_model=schemas.StateOut)
    def get_state():
        out = _state_out()
        if out is None:
            raise HTTPException(503, "no state published yet")
        return out

    @app.get("/report")
    def get_report(date: str | None = None, range: str = "day"):
        from datetime import datetime
        from ..report import build
        day = datetime.fromisoformat(date).astimezone() if date \
            else datetime.now().astimezone()
        conn = db.connect_readonly(db_path)
        try:
            r = build(conn, cfg, day)
            return {
                "date": r.day.date().isoformat(),
                "on_task_share": r.on_task_share,
                "totals": r.totals,
                "sessions": [
                    {"id": s.id, "intent": s.declared_intent,
                     "planned_minutes": s.planned_minutes,
                     "outcome": s.outcome,
                     "started_at": s.started_at.isoformat(),
                     "ended_at": s.ended_at.isoformat() if s.ended_at else None}
                    for s in r.sessions],
                "by_app": r.by_app,
                "drift_starts": r.drift_starts,
                "weakest_hours": r.weakest_hours,
                "llm": r.llm,
            }
        finally:
            conn.close()

    @app.get("/reminders", response_model=list[schemas.ReminderOut])
    def list_reminders():
        from ..reminders import store as reminder_store
        conn = db.connect_readonly(db_path)
        try:
            return [
                schemas.ReminderOut(
                    id=r.id, text=r.text,
                    due_at=r.due_at.isoformat() if r.due_at else None,
                    trigger_context=r.trigger_context, status=r.status)
                for r in reminder_store.pending(conn)]
        finally:
            conn.close()

    @app.get("/doctor", response_model=schemas.DoctorOut)
    def doctor():
        snap = state_bus.latest()
        sock = paths.socket_path()
        schema_version = None
        db_status = "ok"
        try:
            conn = db.connect_readonly(db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()
                schema_version = int(row["value"]) if row else None
            finally:
                conn.close()
        except Exception as e:
            db_status = f"error: {e}"
        return schemas.DoctorOut(
            socket=str(sock),
            db=db_status,
            schema_version=schema_version,
            watcher=snap.watcher if snap else "(no tick yet)",
            xwayland=bool(os.environ.get("DISPLAY")),
            tier3_calls_today=(snap.counters.get("tier3_calls", 0) if snap else 0),
            tier3_cost_usd_today=(snap.counters.get("tier3_cost_usd", 0.0) if snap else 0.0),
            tier3_budget_per_day=cfg.llm.max_calls_per_day,
            connected=snap is not None,
        )

    # --- mutations (every one is a command) --------------------------------

    @app.post("/sessions", response_model=schemas.Accepted)
    def start_session(body: schemas.SessionCreate):
        minutes = body.minutes or cfg.session.default_minutes
        _wait(command_queue.submit("start_session", intent=body.intent,
                                   minutes=minutes, task_id=body.task_id))
        return _accepted(f"session started: {body.intent}")

    # --- tasks (izy-v2.md §3) ----------------------------------------------

    @app.get("/tasks")
    def list_tasks(status: str | None = None, quadrant: str | None = None,
                   parent_id: int | None = None):
        from datetime import datetime
        from .. import tasks as task_store
        conn = db.connect_readonly(db_path)
        try:
            pid = -1 if parent_id is None else parent_id
            now = datetime.now().astimezone()
            out = []
            for t in task_store.list_tasks(conn, status=status, quadrant=quadrant,
                                           parent_id=pid):
                d = t.to_dict()
                d["looks_urgent"] = task_store.looks_urgent(t, now)
                d["stale"] = task_store.is_stale_q4(t, now)
                out.append(d)
            return out
        finally:
            conn.close()

    @app.get("/tasks/{task_id}")
    def get_task(task_id: int):
        from .. import tasks as task_store
        conn = db.connect_readonly(db_path)
        try:
            t = task_store.get(conn, task_id)
            if t is None:
                raise HTTPException(404, "no such task")
            return t.to_dict()
        finally:
            conn.close()

    @app.post("/tasks", response_model=schemas.Accepted)
    def create_task(body: schemas.TaskCreate):
        result = _wait(command_queue.submit(
            "create_task", **body.model_dump(exclude_none=True)))
        return schemas.Accepted(ok=True, detail="task created",
                                task=result, state=_state_out())

    @app.patch("/tasks/{task_id}", response_model=schemas.Accepted)
    def patch_task(task_id: int, body: schemas.TaskUpdate):
        fields = body.model_dump(exclude_none=True)
        result = _wait(command_queue.submit("update_task", task_id=task_id, **fields))
        if result is None:
            raise HTTPException(404, "no such task")
        return schemas.Accepted(ok=True, detail="task updated", task=result)

    @app.post("/tasks/{task_id}/quadrant", response_model=schemas.Accepted)
    def set_quadrant(task_id: int, body: schemas.QuadrantIn):
        result = _wait(command_queue.submit("set_task_quadrant", task_id=task_id,
                                            quadrant=body.quadrant))
        if result is None:
            raise HTTPException(404, "no such task")
        return schemas.Accepted(ok=True, detail=f"moved to {body.quadrant}",
                                task=result)

    @app.post("/tasks/reorder", response_model=schemas.Accepted)
    def reorder(body: schemas.ReorderIn):
        result = _wait(command_queue.submit("reorder_task", task_id=body.task_id,
                                            before=body.before, after=body.after))
        return schemas.Accepted(ok=True, detail="reordered", task=result)

    @app.post("/tasks/{task_id}/hints", response_model=schemas.Accepted)
    def accept_hint(task_id: int, body: schemas.HintIn):
        result = _wait(command_queue.submit(
            "accept_hint", task_id=task_id, app=body.app, domain=body.domain,
            keyword=body.keyword))
        if result is None:
            raise HTTPException(404, "no such task")
        return schemas.Accepted(ok=True, detail="hint added", task=result)

    @app.delete("/tasks/{task_id}", response_model=schemas.Accepted)
    def delete_task(task_id: int):
        _wait(command_queue.submit("delete_task", task_id=task_id))
        return schemas.Accepted(ok=True, detail="task deleted")

    @app.post("/sessions/current/stop", response_model=schemas.Accepted)
    def stop_session(body: schemas.StopIn = Body(default=schemas.StopIn())):
        _wait(command_queue.submit("end_session", outcome=body.outcome))
        return _accepted("session stopped")

    @app.post("/sessions/current/outcome", response_model=schemas.Accepted)
    def session_outcome(body: schemas.OutcomeIn):
        _wait(command_queue.submit("record_outcome", session_id=body.session_id,
                                   outcome=body.outcome))
        return _accepted(f"outcome recorded: {body.outcome}")

    @app.post("/reminders", response_model=schemas.Accepted)
    def add_reminder(body: schemas.ReminderIn):
        _wait(command_queue.submit("add_reminder", raw=body.text))
        return _accepted("reminder submitted")

    @app.post("/interrupts/{reminder_id}/{action}", response_model=schemas.Accepted)
    def interrupt_action(reminder_id: int, action: str):
        # Phase 1: interrupts are reminder-backed. Drift and self-label acks
        # arrive with the arbiter in Phase 2, which gives every interrupt a
        # real id and a shared ack path.
        name = {"ack": "reminder_done", "done": "reminder_done",
                "snooze": "reminder_snooze",
                "dismiss": "reminder_dismiss"}.get(action)
        if name is None:
            raise HTTPException(400, f"unknown interrupt action: {action}")
        _wait(command_queue.submit(name, reminder_id=reminder_id))
        return _accepted(f"reminder {reminder_id} {action}")

    # --- not-yet-built (named, not faked) ----------------------------------

    async def _stub(request):
        # Raw Starlette route (not an api_route), so the Request is passed
        # positionally and FastAPI does not try to validate it as a query field.
        path = request.url.path
        for prefix, phase in _NOT_YET.items():
            if path == prefix or path.startswith(prefix + "/"):
                return JSONResponse(status_code=501,
                                    content={"detail": f"{prefix} arrives in {phase}"})
        return JSONResponse(status_code=404, content={"detail": "not found"})

    methods = ["GET", "POST", "PATCH", "DELETE"]
    for prefix in _NOT_YET:
        app.add_route(prefix, _stub, methods=methods)
        app.add_route(prefix + "/{rest:path}", _stub, methods=methods)

    # --- events stream -----------------------------------------------------

    @app.websocket("/events")
    async def events(ws: WebSocket):
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        token = state_bus.subscribe(queue, loop)
        try:
            while True:
                snap = await queue.get()
                await ws.send_json(snap.to_dict())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state_bus.unsubscribe(token)

    return app


class ApiServer:
    """Runs the app with uvicorn on the UDS, in its own thread.

    Isolated by design: the thread owns only the asyncio loop and the server;
    the tick and the DB writer are elsewhere. If this thread dies, tracking is
    untouched — that is the Phase 1 gate.
    """

    def __init__(self, app, sock_path: Path | None = None) -> None:
        import uvicorn
        self.sock_path = Path(sock_path or paths.socket_path())
        self._app = app
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        import uvicorn
        # A stale socket from a hard crash would make bind() fail; clear it.
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass
        self.sock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = uvicorn.Config(self._app, uds=str(self.sock_path),
                                log_level="warning", access_log=False,
                                lifespan="off")
        self._server = uvicorn.Server(config)
        # uvicorn installs signal handlers by default, which only works on the
        # main thread; the daemon owns those for its own shutdown.
        self._server.install_signal_handlers = False
        self._thread = threading.Thread(target=self._run, name="izy-api",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._server.run()
        except Exception:
            log.exception("api server crashed")
        finally:
            try:
                if self.sock_path.exists():
                    self.sock_path.unlink()
            except OSError:
                pass

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server is not None and getattr(self._server, "started", False) \
                    and self.sock_path.exists():
                try:
                    os.chmod(self.sock_path, 0o600)
                except OSError:
                    pass
                return True
            time.sleep(0.05)
        return False

    def stop(self, timeout: float = 3.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout)
