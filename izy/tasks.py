"""Tasks and the Eisenhower matrix (izy-v2.md §3).

The quadrant is **derived, never stored** — it is just the two flags read two
ways, so there is never a stored quadrant that disagrees with the flags a drag
set. Everything else is ordinary CRUD over one table, plus the two things that
make tasks worth having in a focus tool rather than a to-do app:

  * `hints` — the apps and domains a task uses. A session started from a task
    loads these as free tier-1/2 classification rules, so its own windows never
    reach the paid tier. This is the integration the phase gate checks.
  * `suggestions` — apps a paid or asked verdict found on-task for a task-backed
    session, waiting for your one-tap accept into `hints`. Izy gets cheaper the
    more you use it.

No Qt, no arbiter: pure store functions over a SQLite connection, so the tick
(the one writer) calls them and the API reads them through a read-only view.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import from_iso, to_iso, utcnow

STATUSES = ("todo", "doing", "done", "dropped")
DUE_SOON_HOURS = 24        # inside this, a due date suggests "looks urgent"
STALE_Q4_DAYS = 14         # a neither-urgent-nor-important task older than this
NEGLECT_Q2_DAYS = 7        # an important-not-urgent task untouched this long


@dataclass
class Task:
    id: int
    title: str
    notes: str | None = None
    urgent: bool = False
    important: bool = False
    status: str = "todo"
    due_at: datetime | None = None
    estimate_pomos: int | None = None
    actual_pomos: int = 0
    hints: dict = field(default_factory=dict)
    suggestions: list = field(default_factory=list)
    parent_id: int | None = None
    sort_key: float = 0.0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def quadrant(self) -> str:
        """Derived, never stored: Q1 urgent&important, Q2 important not urgent,
        Q3 urgent not important, Q4 neither."""
        if self.important and self.urgent:
            return "Q1"
        if self.important and not self.urgent:
            return "Q2"
        if self.urgent and not self.important:
            return "Q3"
        return "Q4"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "notes": self.notes,
            "urgent": self.urgent, "important": self.important,
            "quadrant": self.quadrant, "status": self.status,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "estimate_pomos": self.estimate_pomos, "actual_pomos": self.actual_pomos,
            "hints": self.hints, "suggestions": self.suggestions,
            "parent_id": self.parent_id, "sort_key": self.sort_key,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


def _row(r) -> Task:
    return Task(
        id=r["id"], title=r["title"], notes=r["notes"],
        urgent=bool(r["urgent"]), important=bool(r["important"]),
        status=r["status"],
        due_at=from_iso(r["due_at"]) if r["due_at"] else None,
        estimate_pomos=r["estimate_pomos"], actual_pomos=r["actual_pomos"],
        hints=_load_json(r["hints"], {}), suggestions=_load_json(r["suggestions"], []),
        parent_id=r["parent_id"], sort_key=r["sort_key"],
        created_at=from_iso(r["created_at"]) if r["created_at"] else None,
        updated_at=from_iso(r["updated_at"]) if r["updated_at"] else None,
        completed_at=from_iso(r["completed_at"]) if r["completed_at"] else None,
    )


def _load_json(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# --- create / read ---------------------------------------------------------

def create(conn, title: str, *, notes=None, urgent=False, important=False,
           due_at=None, estimate_pomos=None, hints=None, parent_id=None,
           now=None) -> Task:
    now = now or utcnow()
    title = (title or "").strip()
    if not title:
        raise ValueError("a task needs a title")
    # New tasks sort to the top of their quadrant: one less than the current min.
    top = conn.execute("SELECT COALESCE(MIN(sort_key), 0) FROM task").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO task(title, notes, urgent, important, due_at, estimate_pomos,"
        " hints, parent_id, sort_key, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (title, notes, int(urgent), int(important),
         to_iso(due_at) if due_at else None, estimate_pomos,
         json.dumps(hints) if hints else None, parent_id, top - 1.0,
         to_iso(now), to_iso(now)))
    return get(conn, cur.lastrowid)


def get(conn, task_id: int) -> Task | None:
    r = conn.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    return _row(r) if r else None


def list_tasks(conn, *, status: str | None = None, quadrant: str | None = None,
               parent_id: int | None = -1) -> list[Task]:
    """List tasks, newest-sorting first. `parent_id=-1` (default) returns only
    top-level tasks; pass an id for a parent's children, or None for all."""
    sql = "SELECT * FROM task"
    where, args = [], []
    if status is not None:
        where.append("status = ?"); args.append(status)
    if parent_id == -1:
        where.append("parent_id IS NULL")
    elif parent_id is not None:
        where.append("parent_id = ?"); args.append(parent_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sort_key ASC, id ASC"
    tasks = [_row(r) for r in conn.execute(sql, args).fetchall()]
    if quadrant is not None:
        tasks = [t for t in tasks if t.quadrant == quadrant]
    return tasks


def children(conn, parent_id: int) -> list[Task]:
    return list_tasks(conn, parent_id=parent_id)


# --- update ----------------------------------------------------------------

_UPDATABLE = {"title", "notes", "urgent", "important", "status", "due_at",
              "estimate_pomos"}


def update(conn, task_id: int, *, now=None, **fields) -> Task | None:
    task = get(conn, task_id)
    if task is None:
        return None
    now = now or utcnow()
    sets, args = [], []
    for key, value in fields.items():
        if key not in _UPDATABLE:
            continue
        if key in ("urgent", "important"):
            value = int(bool(value))
        elif key == "due_at":
            value = to_iso(value) if value else None
        elif key == "status" and value not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        sets.append(f"{key} = ?"); args.append(value)
    if not sets:
        return task
    sets.append("updated_at = ?"); args.append(to_iso(now))
    if fields.get("status") == "done":
        sets.append("completed_at = ?"); args.append(to_iso(now))
    args.append(task_id)
    conn.execute(f"UPDATE task SET {', '.join(sets)} WHERE id = ?", args)
    return get(conn, task_id)


def set_quadrant(conn, task_id: int, quadrant: str, *, now=None) -> Task | None:
    """Dragging a card between quadrants is the *only* automatic flag change
    (izy-v2.md §3). Everything else is an explicit edit."""
    flags = {"Q1": (1, 1), "Q2": (0, 1), "Q3": (1, 0), "Q4": (0, 0)}
    if quadrant not in flags:
        raise ValueError(f"unknown quadrant: {quadrant}")
    urgent, important = flags[quadrant]
    return update(conn, task_id, urgent=urgent, important=important, now=now)


def complete(conn, task_id: int, *, now=None) -> Task | None:
    return update(conn, task_id, status="done", now=now)


def delete(conn, task_id: int) -> None:
    conn.execute("DELETE FROM task WHERE id = ?", (task_id,))   # cascades to children


def reorder(conn, task_id: int, *, before: int | None = None,
            after: int | None = None) -> Task | None:
    """Move a task to sit between two others by fractional sort_key — no bulk
    renumber, so a drag is one row write."""
    def key_of(tid):
        r = conn.execute("SELECT sort_key FROM task WHERE id = ?", (tid,)).fetchone()
        return r[0] if r else None
    lo = key_of(after) if after else None
    hi = key_of(before) if before else None
    if lo is not None and hi is not None:
        new_key = (lo + hi) / 2.0
    elif hi is not None:
        new_key = hi - 1.0
    elif lo is not None:
        new_key = lo + 1.0
    else:
        return get(conn, task_id)
    conn.execute("UPDATE task SET sort_key = ? WHERE id = ?", (new_key, task_id))
    return get(conn, task_id)


# --- hints (the integration that earns its keep) ---------------------------

def get_hints(conn, task_id: int) -> dict:
    task = get(conn, task_id)
    return task.hints if task else {}


def add_hint(conn, task_id: int, *, app: str | None = None,
             domain: str | None = None, keyword: str | None = None,
             now=None) -> Task | None:
    """Add an app/domain/keyword to a task's hints, and drop it from the
    suggestions if it was one. Idempotent."""
    task = get(conn, task_id)
    if task is None:
        return None
    hints = dict(task.hints or {})
    for field_name, value in (("apps", app), ("domains", domain),
                              ("keywords", keyword)):
        if not value:
            continue
        lst = list(hints.get(field_name, []))
        if value.lower() not in [x.lower() for x in lst]:
            lst.append(value)
        hints[field_name] = lst
    suggestions = [s for s in (task.suggestions or [])
                   if s.lower() not in {x for x in (app or "", domain or "") if x}]
    conn.execute(
        "UPDATE task SET hints = ?, suggestions = ?, updated_at = ? WHERE id = ?",
        (json.dumps(hints), json.dumps(suggestions),
         to_iso(now or utcnow()), task_id))
    return get(conn, task_id)


def suggest_hint(conn, task_id: int, candidate: str, *, now=None) -> None:
    """Record an app/domain a paid or asked verdict found on-task for this task,
    for later one-tap acceptance. Never adds it to hints itself — that is your
    call (izy-v2.md §3: 'offer', not automatic)."""
    task = get(conn, task_id)
    if task is None or not candidate:
        return
    known = {a.lower() for a in (task.hints or {}).get("apps", [])} | \
            {d.lower() for d in (task.hints or {}).get("domains", [])}
    if candidate.lower() in known:
        return
    suggestions = list(task.suggestions or [])
    if candidate.lower() in [s.lower() for s in suggestions]:
        return
    suggestions.append(candidate)
    conn.execute("UPDATE task SET suggestions = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(suggestions), to_iso(now or utcnow()), task_id))


# --- derived signals for the UI (dashboard renders these) ------------------

def looks_urgent(task: Task, now: datetime) -> bool:
    """A due date inside 24 h suggests Q1 — a chip, never an automatic move."""
    return (task.due_at is not None and not task.urgent
            and task.status in ("todo", "doing")
            and task.due_at - now <= timedelta(hours=DUE_SOON_HOURS))


def is_stale_q4(task: Task, now: datetime) -> bool:
    return (task.quadrant == "Q4" and task.status in ("todo", "doing")
            and task.created_at is not None
            and now - task.created_at >= timedelta(days=STALE_Q4_DAYS))


def neglected_q2(conn, now: datetime) -> Task | None:
    """The point of the whole matrix: an important-not-urgent task with no
    session in NEGLECT_Q2_DAYS. Returns one to surface, or None."""
    cutoff = to_iso(now - timedelta(days=NEGLECT_Q2_DAYS))
    for task in list_tasks(conn, status="todo", quadrant="Q2"):
        last = conn.execute(
            "SELECT MAX(started_at) FROM sessions WHERE task_id = ?", (task.id,)
        ).fetchone()[0]
        if last is None or last < cutoff:
            return task
    return None
