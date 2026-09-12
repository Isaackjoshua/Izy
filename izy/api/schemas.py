"""Typed request/response models for the control-plane API.

Pydantic models, so the schema is generated and shareable with the PySide6
clients (izy-v2.md §1: "typed Pydantic models shared with the clients via a
generated JSON schema"). Response models mirror `ipc.StateSnapshot` and the
read helpers; request models validate what the clients may ask for.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

OUTCOMES = Literal["finished", "partly", "no"]


class SessionOut(BaseModel):
    id: int
    intent: str
    planned_minutes: int
    elapsed_s: int
    remaining_s: int


class StateOut(BaseModel):
    tick: int
    ts: str
    mascot: str
    phase: str
    watcher: str
    session: Optional[SessionOut] = None
    focus_app: Optional[str] = None
    focus_title: Optional[str] = None
    counters: dict = Field(default_factory=dict)
    connected: bool = True


class SessionCreate(BaseModel):
    intent: str = Field(min_length=1)
    minutes: Optional[int] = Field(default=None, ge=1, le=600)
    task_id: Optional[int] = None      # honoured from Phase 3; ignored until then


class StopIn(BaseModel):
    outcome: Optional[OUTCOMES] = None


class OutcomeIn(BaseModel):
    outcome: OUTCOMES
    session_id: Optional[int] = None


class ReminderIn(BaseModel):
    text: str = Field(min_length=1)


class ReminderOut(BaseModel):
    id: int
    text: str
    due_at: Optional[str] = None
    trigger_context: Optional[str] = None
    status: str


class Accepted(BaseModel):
    """Returned by mutations: the command was accepted and applied on the tick,
    with the resulting state attached so a client need not immediately re-GET."""
    ok: bool = True
    detail: Optional[str] = None
    state: Optional[StateOut] = None


class DoctorOut(BaseModel):
    socket: str
    db: str
    schema_version: Optional[int] = None
    watcher: str
    xwayland: bool
    tier3_calls_today: int
    tier3_cost_usd_today: float
    tier3_budget_per_day: int
    connected: bool
