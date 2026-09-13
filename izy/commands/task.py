"""`izy task` — create and manage tasks from the terminal (izy-v2.md §3).

Tasks are not in-memory daemon state the way a session is, so the CLI writes
them straight to SQLite and the daemon reads them fresh — no resync needed. A
session started from a task (`izy start --task N`) does flow through the daemon,
which loads the task's hints on resync.
"""
from __future__ import annotations

import sys

from .. import config, db
from .. import tasks as task_store

_QUADRANT = {"Q1": "urgent+important", "Q2": "important", "Q3": "urgent",
             "Q4": "neither"}


def _fmt(t) -> str:
    flags = t.quadrant
    hints = t.hints or {}
    hint_bits = []
    if hints.get("apps"):
        hint_bits.append("apps=" + ",".join(hints["apps"]))
    if hints.get("domains"):
        hint_bits.append("domains=" + ",".join(hints["domains"]))
    hint_str = f"  hints[{'; '.join(hint_bits)}]" if hint_bits else ""
    sugg = f"  suggest[{','.join(t.suggestions)}]" if t.suggestions else ""
    return (f"  {t.id:>3} [{flags}] {t.status:<7} {t.title[:48]:<48}"
            f"{hint_str}{sugg}")


def cmd_task(args) -> int:
    conn = db.connect()
    action = args.action

    if action == "list":
        tasks = task_store.list_tasks(conn, status=args.status)
        if not tasks:
            print("no tasks")
            return 0
        for q in ("Q1", "Q2", "Q3", "Q4"):
            group = [t for t in tasks if t.quadrant == q]
            if group:
                print(f"{q} ({_QUADRANT[q]}):")
                for t in group:
                    print(_fmt(t))
        return 0

    if action == "add":
        t = task_store.create(
            conn, args.title, urgent=args.urgent, important=args.important,
            estimate_pomos=args.pomos,
            hints={"apps": args.hint_app or [], "domains": args.hint_domain or []}
            if (args.hint_app or args.hint_domain) else None)
        print(f"created task {t.id} [{t.quadrant}]: {t.title!r}")
        return 0

    if action == "done":
        t = task_store.complete(conn, args.id)
        if t is None:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        print(f"task {args.id} done")
        return 0

    if action == "rm":
        if task_store.get(conn, args.id) is None:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        task_store.delete(conn, args.id)
        print(f"task {args.id} deleted")
        return 0

    if action == "quadrant":
        t = task_store.set_quadrant(conn, args.id, args.value)
        if t is None:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        print(f"task {args.id} -> {t.quadrant}")
        return 0

    if action == "hint":
        t = task_store.add_hint(conn, args.id, app=args.app, domain=args.domain,
                                keyword=args.keyword)
        if t is None:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        print(f"task {args.id} hints: {t.hints}")
        return 0

    print(f"unknown task action: {action}", file=sys.stderr)
    return 2
