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
    st.set_defaults(func=session_cmd.cmd_start)

    sp = sub.add_parser("stop", help="end the open focus session")
    sp.add_argument("outcome", nargs="?", choices=["finished", "partly", "no"])
    sp.set_defaults(func=session_cmd.cmd_stop)

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
