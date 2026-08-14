"""Command line: dump the day's log, check the environment, install the
shell extension, manage sessions without touching the mascot.

Reads the same SQLite file the daemon writes (WAL, so concurrent reads are
fine) and never starts Qt, so `izy day` works over SSH and in a headless test.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, paths
from .models import from_iso, utcnow

EXT_UUID = "izy@local"


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _local(ts: str) -> str:
    return from_iso(ts).astimezone().strftime("%H:%M:%S")


def _parse_day(s: str | None) -> datetime:
    if not s or s == "today":
        return datetime.now().astimezone()
    if s == "yesterday":
        return datetime.now().astimezone() - timedelta(days=1)
    return datetime.fromisoformat(s).astimezone()


# --- commands ---------------------------------------------------------------

def cmd_day(args) -> int:
    conn = db.connect()
    day = _parse_day(args.date)
    events = db.events_for_day(conn, day)
    sessions = db.sessions_for_day(conn, day)
    labels = db.labels_for_day(conn, day)

    if args.json:
        print(json.dumps({
            "date": day.date().isoformat(),
            "sessions": [
                {"id": s.id, "started_at": s.started_at.isoformat(),
                 "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                 "intent": s.declared_intent, "planned_minutes": s.planned_minutes,
                 "outcome": s.outcome}
                for s in sessions],
            "events": [dict(e) for e in events],
            "labels": [dict(l) for l in labels],
        }, indent=2))
        return 0

    print(f"=== {day.date().isoformat()} ===\n")

    print(f"Sessions ({len(sessions)})")
    if not sessions:
        print("  none")
    for s in sessions:
        actual = ((s.ended_at or utcnow()) - s.started_at).total_seconds() / 60
        end = s.ended_at.astimezone().strftime("%H:%M") if s.ended_at else "open"
        print(f"  {s.started_at.astimezone():%H:%M}-{end}  {s.declared_intent[:44]:<44} "
              f"planned {s.planned_minutes}m / actual {actual:.0f}m  "
              f"outcome={s.outcome or '-'}")

    total = sum(e["duration_s"] for e in events)
    afk = sum(e["duration_s"] for e in events if e["afk"])
    print(f"\nActivity  {len(events)} spans, {_fmt_dur(total)} tracked "
          f"({_fmt_dur(afk)} afk)")

    by_app: dict[str, float] = {}
    for e in events:
        if e["afk"]:
            continue
        by_app[e["app"] or "?"] = by_app.get(e["app"] or "?", 0) + e["duration_s"]
    for app, secs in sorted(by_app.items(), key=lambda kv: -kv[1])[: args.top]:
        print(f"  {_fmt_dur(secs):>8}  {app}")

    if args.verbose:
        print("\nSpans")
        for e in events:
            tag = "afk" if e["afk"] else (e["app"] or "?")
            print(f"  {_local(e['ts'])}  {_fmt_dur(e['duration_s']):>8}  "
                  f"{tag[:18]:<18} {(e['window_title'] or '')[:60]}")

    print(f"\nLabels ({len(labels)})")
    for l in labels:
        mark = "on-task " if l["on_task"] else "off-task"
        print(f"  {_local(l['created_at'])}  {mark}  [{l['source']}]  "
              f"{(l['window_title'] or l['app'] or '')[:56]}")
    return 0


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


def cmd_doctor(args) -> int:
    """Re-check the Step 0 conditions on demand. Cheap to run, and the first
    thing to try when titles stop showing up."""
    from .watchers import pick_watcher
    from .watchers.native import IZY_DEST
    from . import dbus

    cfg = config.load()
    print(f"session type   : {_env('XDG_SESSION_TYPE')}")
    print(f"desktop        : {_env('XDG_CURRENT_DESKTOP')}")
    ext = dbus.service_available(IZY_DEST)
    print(f"izy extension  : {'present on the bus' if ext else 'NOT RUNNING'}")
    if ext:
        print(f"  focus sample : {dbus.call_string(IZY_DEST, '/org/izy/Focus', IZY_DEST, 'GetFocused')}")
    installed = (Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID).exists()
    print(f"  installed    : {installed}")
    if installed and not ext:
        print("  -> installed but not loaded. Enable it and log out and back in:")
        print(f"     gnome-extensions enable {EXT_UUID}")
    if not installed:
        print("  -> run: izy install-extension")

    watcher = pick_watcher(cfg.watcher)
    describe = getattr(watcher, "describe", None)
    print(f"chosen watcher : {describe() if describe else watcher.name}")
    snap = watcher.poll()
    print(f"poll now       : {snap}")
    watcher.close()
    return 0 if snap is not None else 1


def cmd_install_extension(args) -> int:
    src = Path(__file__).resolve().parent.parent / "gnome-extension" / EXT_UUID
    if not src.exists():
        print(f"extension source not found at {src}", file=sys.stderr)
        return 1
    dest = Path.home() / ".local/share/gnome-shell/extensions" / EXT_UUID
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    print(f"installed -> {dest}")

    if shutil.which("gnome-extensions"):
        subprocess.run(["gnome-extensions", "enable", EXT_UUID],
                       capture_output=True, text=True)
    print(
        "\nGNOME Shell only scans for new extensions at startup, and a Wayland\n"
        "session cannot restart the shell in place. Log out and back in once,\n"
        "then confirm with:  izy doctor\n"
        f"If it is still not loaded after that:  gnome-extensions enable {EXT_UUID}"
    )
    return 0


def cmd_start(args) -> int:
    conn = db.connect()
    cfg = config.load()
    from .sessions import SessionManager
    sm = SessionManager(conn, cfg)
    sm.recover()
    s = sm.start(args.intent, args.minutes)
    print(f"session {s.id} started: {s.declared_intent!r} ({s.planned_minutes}m)")
    return 0


def cmd_stop(args) -> int:
    conn = db.connect()
    cfg = config.load()
    from .sessions import SessionManager
    sm = SessionManager(conn, cfg)
    if not sm.recover():
        print("no open session")
        return 1
    ended = sm.end(args.outcome)
    print(f"session {ended.id} ended (outcome={ended.outcome or '-'})")
    return 0


def _env(name: str) -> str:
    import os
    return os.environ.get(name) or "(unset)"


# --- entry point ------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="izy", description="Desktop focus companion")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("day", help="dump a day's activity log")
    d.add_argument("date", nargs="?", default="today",
                   help="today | yesterday | YYYY-MM-DD")
    d.add_argument("-v", "--verbose", action="store_true", help="list every span")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--top", type=int, default=12, help="how many apps to summarise")
    d.set_defaults(func=cmd_day)

    s = sub.add_parser("status", help="what is Izy doing right now")
    s.set_defaults(func=cmd_status)

    doc = sub.add_parser("doctor", help="re-check that activity tracking works")
    doc.set_defaults(func=cmd_doctor)

    ie = sub.add_parser("install-extension", help="install the GNOME shell extension")
    ie.set_defaults(func=cmd_install_extension)

    st = sub.add_parser("start", help="start a focus session")
    st.add_argument("intent", help="what you are working on")
    st.add_argument("-m", "--minutes", type=int, default=None)
    st.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="end the open focus session")
    sp.add_argument("outcome", nargs="?", choices=["finished", "partly", "no"])
    sp.set_defaults(func=cmd_stop)

    run = sub.add_parser("run", help="run the daemon in the foreground")
    run.set_defaults(func=lambda a: __import__("izy.app", fromlist=["run"]).run([sys.argv[0]]))
    return p


def main(argv: list[str] | None = None) -> int:
    paths.ensure_dirs()
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
