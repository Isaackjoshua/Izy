"""Command line entry point.

Deliberately thin: this file wires argparse to the command functions in
`izy/commands/` and does nothing else. It never starts Qt, so `izy day` works
over SSH and in a headless test, and it reads the same SQLite file the daemon
writes (WAL, so concurrent reads are fine).
"""
from __future__ import annotations

import argparse
import sys

from . import paths
from .commands import day as day_cmd
from .commands import config_cmd
from .commands import env as env_cmd
from .commands import reminder as reminder_cmd
from .commands import report as report_cmd
from .commands import session as session_cmd
from .commands import task as task_cmd

#: Re-exported so `from izy.cli import EXT_UUID` keeps working.
EXT_UUID = env_cmd.EXT_UUID


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="izy", description="Desktop focus companion")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("day", help="dump a day's activity log")
    d.add_argument("date", nargs="?", default="today",
                   help="today | yesterday | YYYY-MM-DD")
    d.add_argument("-v", "--verbose", action="store_true", help="list every span")
    d.add_argument("--json", action="store_true", help="machine-readable output")
    d.add_argument("--top", type=int, default=12, help="how many apps to summarise")
    d.set_defaults(func=day_cmd.cmd_day)

    rep = sub.add_parser("report", help="build and open the day's retrospective")
    rep.add_argument("date", nargs="?", default="today",
                     help="today | yesterday | YYYY-MM-DD")
    rep.add_argument("--no-serve", action="store_true",
                     help="just write the HTML file; corrections then need izy relabel")
    rep.add_argument("--no-open", action="store_true",
                     help="serve but do not launch a browser")
    rep.set_defaults(func=report_cmd.cmd_report)

    rel = sub.add_parser("relabel", help="correct a classification")
    rel.add_argument("event_id", type=int)
    rel.add_argument("verdict", choices=["on", "off"])
    rel.set_defaults(func=day_cmd.cmd_relabel)

    rm = sub.add_parser("remind", help="add a reminder, e.g. izy remind me to X at 4pm")
    rm.add_argument("text", nargs="+", help="the reminder, in plain English")
    rm.set_defaults(func=reminder_cmd.cmd_remind)

    rl = sub.add_parser("reminders", help="list pending reminders")
    rl.set_defaults(func=reminder_cmd.cmd_reminders)

    s = sub.add_parser("status", help="what Izy is doing right now")
    s.set_defaults(func=session_cmd.cmd_status)

    st = sub.add_parser("start", help="start a focus session")
    st.add_argument("intent", help="what you are working on")
    st.add_argument("-m", "--minutes", type=int, default=None)
    st.add_argument("--task", type=int, default=None,
                    help="start from a task, loading its hints for free classification")
    st.set_defaults(func=session_cmd.cmd_start)

    sp = sub.add_parser("stop", help="end the open focus session")
    sp.add_argument("outcome", nargs="?", choices=["finished", "partly", "no"])
    sp.set_defaults(func=session_cmd.cmd_stop)

    tk = sub.add_parser("task", help="create and manage tasks (Eisenhower matrix)")
    tksub = tk.add_subparsers(dest="action", required=True)
    tadd = tksub.add_parser("add", help="add a task")
    tadd.add_argument("title")
    tadd.add_argument("-u", "--urgent", action="store_true")
    tadd.add_argument("-i", "--important", action="store_true")
    tadd.add_argument("--pomos", type=int, default=None, help="estimate in pomodoros")
    tadd.add_argument("--hint-app", action="append", help="app this task uses (repeatable)")
    tadd.add_argument("--hint-domain", action="append", help="domain this task uses (repeatable)")
    tls = tksub.add_parser("list", help="list tasks by quadrant")
    tls.add_argument("--status", choices=["todo", "doing", "done", "dropped"], default=None)
    tdone = tksub.add_parser("done", help="mark a task done"); tdone.add_argument("id", type=int)
    trm = tksub.add_parser("rm", help="delete a task"); trm.add_argument("id", type=int)
    tq = tksub.add_parser("quadrant", help="move a task between quadrants")
    tq.add_argument("id", type=int); tq.add_argument("value", choices=["Q1","Q2","Q3","Q4"])
    thint = tksub.add_parser("hint", help="add a hint to a task")
    thint.add_argument("id", type=int)
    thint.add_argument("--app"); thint.add_argument("--domain"); thint.add_argument("--keyword")
    tk.set_defaults(func=task_cmd.cmd_task)

    cf = sub.add_parser("config", help="show the config file and whether it is current")
    cf.add_argument("--upgrade", action="store_true",
                    help="add missing sections and keys, preserving your edits")
    cf.set_defaults(func=config_cmd.cmd_config)

    doc = sub.add_parser("doctor", help="re-check that activity tracking works")
    doc.set_defaults(func=env_cmd.cmd_doctor)

    ie = sub.add_parser("install-extension", help="install the GNOME shell extension")
    ie.set_defaults(func=env_cmd.cmd_install_extension)

    run = sub.add_parser("run", help="run the daemon in the foreground")
    run.set_defaults(
        func=lambda a: __import__("izy.app", fromlist=["run"]).run([sys.argv[0]]))
    return p


def main(argv: list[str] | None = None) -> int:
    paths.ensure_dirs()
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
