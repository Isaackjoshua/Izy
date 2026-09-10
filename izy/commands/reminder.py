"""`izy remind` and `izy reminders`."""
from __future__ import annotations

import sys

from .. import config, db

def cmd_remind(args) -> int:
    """Add a reminder from the command line. Same parser the mascot uses."""
    from ..llm import LLM
    from ..reminders import parse as parse_reminder
    from ..reminders import store as reminder_store

    conn = db.connect()
    cfg = config.load()
    # argparse has already eaten the word "remind" as the subcommand, so
    # `izy remind me to X at 4pm` arrives as "me to X at 4pm". Put it back,
    # otherwise the parser's prefix stripping leaves a stranded "me".
    raw = " ".join(args.text).strip()
    if not raw.lower().startswith("remind"):
        raw = f"remind {raw}" if raw.lower().startswith("me ") else f"remind me {raw}"
    parsed = parse_reminder(raw, LLM(conn, cfg))
    if not parsed.is_valid():
        print(f"could not read a time or trigger in: {' '.join(args.text)!r}",
              file=sys.stderr)
        print("try: 'in 20 minutes', 'at 4pm', 'on my next break'", file=sys.stderr)
        return 1
    text = f"[urgent] {parsed.text}" if parsed.urgent else parsed.text
    from dataclasses import replace as _replace
    r = reminder_store.add(conn, _replace(parsed, text=text))
    when = (r.due_at.astimezone().strftime("%a %H:%M") if r.due_at
            else _humanize_context(r.trigger_context))
    print(f"reminder {r.id}: {r.text!r} -> {when}  [{parsed.source}]")
    return 0


_CONTEXT_LABELS = {
    "on_break": "next break",
    "session_start": "session start",
    "session_end": "session end",
    "end_of_day": "end of day",
}


def _humanize_context(context: str | None) -> str:
    if not context:
        return "-"
    if context.startswith("app_opened:"):
        return f"opening {context.split(':', 1)[1]}"
    return _CONTEXT_LABELS.get(context, context)


def cmd_reminders(args) -> int:
    from ..reminders import store as reminder_store

    conn = db.connect()
    items = reminder_store.pending(conn)
    if not items:
        print("no pending reminders")
        return 0
    print(f"Pending ({len(items)})")
    for r in items:
        when = (r.due_at.astimezone().strftime("%a %H:%M") if r.due_at
                else _humanize_context(r.trigger_context))
        flag = " (snoozed)" if r.status == "snoozed" else ""
        print(f"  {r.id:>3}  {when:<14} {r.text}{flag}")
    return 0
