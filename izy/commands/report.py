"""`izy report` — build the retrospective dashboard and serve it."""
from __future__ import annotations

from .. import config, db
from .fmt import _parse_day

def cmd_report(args) -> int:
    """Build the day's retrospective and open it.

    Served from localhost by default so the "this was wrong" buttons can write
    back; --no-serve just writes the file for later.
    """
    from .. import report as report_mod
    from ..report.serve import ReportServer

    cfg = config.load()
    conn = db.connect()
    day = _parse_day(args.date)
    path = report_mod.write(conn, cfg, day)
    conn.close()
    print(f"wrote {path}")

    if args.no_serve:
        print(f"open it with:  xdg-open {path}")
        return 0

    server = ReportServer(db.connect, cfg, day)
    url = server.url
    print(f"serving at {url}   (ctrl-c to stop)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    server.serve_forever()
    return 0
