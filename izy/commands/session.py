"""`izy status`, `izy start`, `izy stop` — sessions without the mascot."""
from __future__ import annotations

from .. import config, db, paths
from ..models import utcnow
from .fmt import _local

def cmd_status(args) -> int:
    conn = db.connect()
    s = db.open_session(conn)
    if s:
        elapsed = (utcnow() - s.started_at).total_seconds() / 60
        print(f"session {s.id} open: {s.declared_intent!r} "
              f"({elapsed:.0f}m of {s.planned_minutes}m)")
    else:
        print("no open session")
    recent = db.recent_events(conn, 1)
    if recent:
        e = recent[0]
        print(f"last event: {_local(e['ts'])} {e['app']} — {e['window_title']}")
    else:
        print("no events recorded yet")
    print(f"db: {paths.db_path()}")
    print(f"config: {paths.config_path()}")
    return 0

def cmd_start(args) -> int:
    conn = db.connect()
    cfg = config.load()
    from ..sessions import SessionManager
    sm = SessionManager(conn, cfg)
    sm.recover()
    s = sm.start(args.intent, args.minutes)
    print(f"session {s.id} started: {s.declared_intent!r} ({s.planned_minutes}m)")
    return 0


def cmd_stop(args) -> int:
    conn = db.connect()
    cfg = config.load()
    from ..sessions import SessionManager
    sm = SessionManager(conn, cfg)
    if not sm.recover():
        print("no open session")
        return 1
    ended = sm.end(args.outcome)
    print(f"session {ended.id} ended (outcome={ended.outcome or '-'})")
    return 0
