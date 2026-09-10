"""The retrospective dashboard — SPEC.md Feature 5.

A local HTML file opened in the browser, not a native GUI. Regenerated on
demand (`izy report`) and automatically at the end of the day.
"""
from .data import DayReport, build
from .render import render

__all__ = ["DayReport", "build", "render", "write", "REPORTS_DIR"]

import logging
from datetime import datetime
from pathlib import Path

from .. import paths

log = logging.getLogger(__name__)


def REPORTS_DIR() -> Path:
    return paths.data_dir() / "reports"


def write(conn, cfg, day: datetime | None = None, path: Path | None = None) -> Path:
    """Build and write the day's dashboard. Returns the file path."""
    report = build(conn, cfg, day)
    out = path or (REPORTS_DIR() / f"{report.day.date().isoformat()}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    log.info("wrote retrospective to %s", out)
    return out
