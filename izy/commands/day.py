"""`izy day` — dump a day's activity log, and `izy relabel` to correct a call."""
from __future__ import annotations

import json
import sys

from .. import config, db
from ..models import utcnow
from .fmt import _fmt_dur, _local, _parse_day

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

    calls = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM llm_calls"
        " WHERE ts >= ? AND ts < ? AND cached = 0", db.day_bounds(day)).fetchone()
    cached = conn.execute(
        "SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND ts < ? AND cached = 1",
        db.day_bounds(day)).fetchone()[0]
    print(f"\nLLM  {calls[0]} paid call(s), ${calls[1]:.4f}, {cached} cache hit(s)")

    by_source: dict[str, int] = {}
    on_task_s = off_task_s = 0.0
    for l in labels:
        by_source[l["source"]] = by_source.get(l["source"], 0) + 1
    for e in events:
        lab = conn.execute(
            "SELECT on_task FROM labels WHERE event_id=? ORDER BY id DESC LIMIT 1",
            (e["id"],)).fetchone()
        if lab is None or e["afk"]:
            continue
        if lab["on_task"]:
            on_task_s += e["duration_s"]
        else:
            off_task_s += e["duration_s"]

    judged = on_task_s + off_task_s
    share = f"{on_task_s / judged * 100:.0f}% on task" if judged else "nothing judged"
    print(f"\nClassification  {_fmt_dur(on_task_s)} on / {_fmt_dur(off_task_s)} off"
          f"  ({share})")
    if by_source:
        print("  by source: " + ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))

    # The audit SPEC.md Feature 5 asks for: every paid and asked decision, with
    # its reason, so a wrong one can be spotted and corrected.
    audit = [l for l in labels if l["source"] in ("llm", "user")]
    if audit:
        print(f"\nAudit ({len(audit)} tier 3/4 decision(s))")
        for l in audit:
            mark = "on-task " if l["on_task"] else "off-task"
            conf = f"{l['confidence']:.2f}" if l["confidence"] is not None else "  - "
            print(f"  {_local(l['created_at'])}  {mark} {conf} [{l['source']}]  "
                  f"{(l['window_title'] or l['app'] or '')[:40]:<40} "
                  f"{(l['reason'] or '')[:40]}")
        print("  wrong? correct it with:  izy relabel <event-id> on|off")

    if args.verbose:
        print(f"\nAll labels ({len(labels)})")
        for l in labels:
            mark = "on-task " if l["on_task"] else "off-task"
            print(f"  {_local(l['created_at'])}  {mark}  [{l['source']}]  "
                  f"{(l['window_title'] or l['app'] or '')[:56]}")
    return 0

def cmd_relabel(args) -> int:
    """Correct a classification. Every correction is a training example, so this
    has to be effortless — SPEC.md Feature 5."""
    conn = db.connect()
    row = conn.execute("SELECT * FROM activity_events WHERE id=?",
                       (args.event_id,)).fetchone()
    if row is None:
        print(f"no event {args.event_id}", file=sys.stderr)
        return 1
    on_task = args.verdict in ("on", "on-task", "true", "1")

    from ..classifier import Classifier
    from ..llm import LLM
    cfg = config.load()
    Classifier(conn, cfg, LLM(conn, cfg)).record_user_answer(args.event_id, on_task)
    print(f"event {args.event_id} ({row['app']} — {row['window_title']}) "
          f"-> {'on-task' if on_task else 'off-task'}")
    return 0
