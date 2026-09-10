"""Gathering a day into the shape the dashboard renders.

Pure reads and arithmetic — no HTML, no I/O beyond SQLite — so the numbers can
be checked in tests without parsing a page.

One honest approximation lives here: breaks are not a table. `SessionManager`
holds `break_until` in memory, and SPEC.md's schema has no breaks table, so a
break band is reconstructed as the configured break length following each ended
session. That is what actually happened, but it is derived rather than recorded,
and `BREAKS_ARE_DERIVED` says so on the page.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import db
from ..models import from_iso, utcnow

BREAKS_ARE_DERIVED = ("Break bands are derived from each session's end plus the "
                      "configured break length, not recorded separately.")

ON, OFF, BREAK, AFK, UNJUDGED = "on", "off", "break", "afk", "unjudged"


@dataclass
class Band:
    start: datetime
    end: datetime
    kind: str
    app: str | None = None
    title: str | None = None
    event_id: int | None = None

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class DayReport:
    day: datetime
    bands: list[Band] = field(default_factory=list)
    sessions: list = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    by_app: list = field(default_factory=list)
    drift_starts: list = field(default_factory=list)
    weakest_hours: list = field(default_factory=list)
    audit: list = field(default_factory=list)
    llm: dict = field(default_factory=dict)

    @property
    def judged_seconds(self) -> float:
        return self.totals.get(ON, 0.0) + self.totals.get(OFF, 0.0)

    @property
    def on_task_share(self) -> float | None:
        judged = self.judged_seconds
        return (self.totals.get(ON, 0.0) / judged) if judged else None

    @property
    def has_data(self) -> bool:
        return bool(self.bands or self.sessions)


def _label_for(conn, event_id: int):
    """The most recent label wins — a user correction supersedes the machine."""
    return conn.execute(
        "SELECT source, on_task, confidence, reason FROM labels"
        " WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)).fetchone()


def build(conn, cfg, day: datetime | None = None) -> DayReport:
    day = day or datetime.now().astimezone()
    report = DayReport(day=day)

    events = db.events_for_day(conn, day)
    sessions = db.sessions_for_day(conn, day)
    report.sessions = sessions

    totals = {ON: 0.0, OFF: 0.0, BREAK: 0.0, AFK: 0.0, UNJUDGED: 0.0}
    by_app: dict[str, float] = {}
    per_hour: dict[int, list[float]] = {}
    # app -> [times it pulled you out, seconds of off-task time that followed].
    # Counting alone is not enough: three apps at "1x" render as three identical
    # full-width bars, which says nothing. Time lost is what varies and what you
    # would act on.
    drift: dict[str, list] = {}
    previous_on_task: bool | None = None
    drift_app: str | None = None

    for row in events:
        start = from_iso(row["ts"])
        end = start + timedelta(seconds=row["duration_s"] or 0)

        seconds = max(0.0, (end - start).total_seconds())

        if row["afk"]:
            kind = AFK
            previous_on_task = None      # away breaks the run without judging it
            drift_app = None
        else:
            label = _label_for(conn, row["id"])
            if label is None:
                kind = UNJUDGED
            else:
                on_task = bool(label["on_task"])
                kind = ON if on_task else OFF
                if on_task:
                    drift_app = None
                else:
                    if previous_on_task is True:
                        # The moment attention left: this app pulled you out,
                        # and the whole run that follows is charged to it.
                        drift_app = row["app"] or "unknown"
                        drift.setdefault(drift_app, [0, 0.0])[0] += 1
                    if drift_app:
                        drift[drift_app][1] += seconds
                previous_on_task = on_task
        totals[kind] += seconds
        if kind in (ON, OFF):
            by_app.setdefault(row["app"] or "unknown", 0.0)
            by_app[row["app"] or "unknown"] += seconds
            hour = start.astimezone().hour
            per_hour.setdefault(hour, [0.0, 0.0])
            per_hour[hour][0 if kind == ON else 1] += seconds

        report.bands.append(Band(start, end, kind, row["app"],
                                 row["window_title"], row["id"]))

    # Derived break bands (see BREAKS_ARE_DERIVED).
    for session in sessions:
        if session.ended_at:
            break_end = session.ended_at + timedelta(
                minutes=cfg.session.break_minutes)
            report.bands.append(Band(session.ended_at, break_end, BREAK))
            totals[BREAK] += cfg.session.break_minutes * 60

    report.bands.sort(key=lambda b: b.start)
    report.totals = totals
    report.by_app = sorted(by_app.items(), key=lambda kv: -kv[1])[:10]

    # (app, times, seconds lost), worst time first.
    report.drift_starts = sorted(
        ((app, count, secs) for app, (count, secs) in drift.items()),
        key=lambda row: -row[2])
    report.weakest_hours = _weakest_hours(per_hour)
    report.audit = _audit(conn, day)
    report.llm = _llm_spend(conn, day)
    return report


def _weakest_hours(per_hour: dict[int, list[float]]) -> list[tuple[int, float, float]]:
    """(hour, off-task share, judged seconds) for every hour with real data.

    Hours with only a few seconds judged are kept but carry their sample size,
    so the page can show that a 100% figure came from 40 seconds.
    """
    out = []
    for hour in sorted(per_hour):
        on, off = per_hour[hour]
        judged = on + off
        if judged <= 0:
            continue
        out.append((hour, off / judged, judged))
    return out


def _audit(conn, day: datetime) -> list[dict]:
    """Every tier 3 and tier 4 decision, newest first — what Feature 5 asks to
    be able to correct in one click."""
    lo, hi = db.day_bounds(day)
    rows = conn.execute(
        "SELECT l.id, l.event_id, l.source, l.on_task, l.confidence, l.reason,"
        " l.created_at, e.app, e.window_title, e.duration_s, s.declared_intent"
        " FROM labels l"
        " JOIN activity_events e ON e.id = l.event_id"
        " LEFT JOIN sessions s ON s.id = e.session_id"
        # Scoped by when the activity happened, not when the label was
        # written: a batch flushed at 00:05 judges yesterday's windows, and
        # those decisions belong in yesterday's audit.
        " WHERE e.ts >= ? AND e.ts < ?"
        " AND l.source IN ('llm', 'user')"
        " ORDER BY l.id DESC", (lo, hi)).fetchall()
    return [dict(r) for r in rows]


def _llm_spend(conn, day: datetime) -> dict:
    lo, hi = db.day_bounds(day)
    paid = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0), COALESCE(SUM(input_tokens), 0),"
        " COALESCE(SUM(output_tokens), 0) FROM llm_calls"
        " WHERE ts >= ? AND ts < ? AND cached = 0", (lo, hi)).fetchone()
    cached = conn.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND ts < ? AND cached = 1",
        (lo, hi)).fetchone()[0]
    return {"calls": paid[0], "cost_usd": paid[1], "input_tokens": paid[2],
            "output_tokens": paid[3], "cache_hits": cached}
