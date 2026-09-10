"""Turning "remind me to email the supervisor at 4pm" into a row.

Order is dateparser first, LLM second, ask third. dateparser handles the common
absolute and relative times for free, so the paid path only sees what it cannot
read. SPEC.md's hard rule: never let the LLM invent a time that wasn't stated —
if it is unclear, ask. `ParsedReminder.needs_confirmation` is how that is said.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import utcnow

log = logging.getLogger(__name__)

#: Context triggers SPEC.md requires at minimum.
CONTEXTS = ("on_break", "session_start", "session_end", "end_of_day")
_APP_CONTEXT = re.compile(r"^app_opened:[\w.\- ]+$")

_PREFIX = re.compile(r"^\s*remind\s+me\s+(?:to\s+)?", re.I)
_URGENT = re.compile(r"\b(urgent|urgently|now|immediately|asap)\b", re.I)

_CONTEXT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bnext time i take a break\b|\bon (?:my )?next break\b|\bwhen i take a break\b", re.I), "on_break"),
    (re.compile(r"\bwhen i start (?:my )?next session\b|\bat session start\b|\bnext session\b", re.I), "session_start"),
    (re.compile(r"\bwhen (?:this|the) session ends\b|\bat session end\b|\bafter this session\b", re.I), "session_end"),
    (re.compile(r"\bat the end of (?:the |my )?day\b|\bend of day\b|\btonight\b", re.I), "end_of_day"),
]
_APP_PATTERN = re.compile(
    r"\bnext time i open ([\w.\- ]{2,30}?)\b(?:\s|,|$)|\bwhen i open ([\w.\- ]{2,30}?)\b(?:\s|,|$)", re.I)


@dataclass(frozen=True)
class ParsedReminder:
    text: str
    kind: str | None = None                  # "time" | "context" | None
    due_at: datetime | None = None
    trigger_context: str | None = None
    urgent: bool = False
    #: True when nothing usable could be extracted. The caller must ask rather
    #: than store a guessed time.
    needs_confirmation: bool = False
    source: str = "rule"                     # rule | dateparser | llm

    def is_valid(self) -> bool:
        if self.kind == "time":
            return self.due_at is not None
        if self.kind == "context":
            return bool(self.trigger_context)
        return False


def strip_prefix(raw: str) -> str:
    return _PREFIX.sub("", raw or "").strip()


def valid_context(value: str | None) -> bool:
    return bool(value) and (value in CONTEXTS or bool(_APP_CONTEXT.match(value)))


def parse_context(raw: str) -> ParsedReminder | None:
    """Context triggers, matched by rule. Free, and covers the phrasings in
    SPEC.md directly."""
    body = strip_prefix(raw)
    for pattern, context in _CONTEXT_PATTERNS:
        if pattern.search(body):
            return ParsedReminder(
                text=_clean(pattern.sub("", body)),
                kind="context", trigger_context=context,
                urgent=bool(_URGENT.search(body)), source="rule")
    m = _APP_PATTERN.search(body)
    if m:
        app = (m.group(1) or m.group(2) or "").strip()
        if app:
            return ParsedReminder(
                text=_clean(_APP_PATTERN.sub("", body)),
                kind="context", trigger_context=f"app_opened:{app.lower()}",
                urgent=bool(_URGENT.search(body)), source="rule")
    return None


def parse_time(raw: str, *, now=None) -> ParsedReminder | None:
    """Absolute and relative times via dateparser. Returns None if it cannot
    read one — never a guess."""
    body = strip_prefix(raw)
    now = now or utcnow()
    try:
        import dateparser
    except ImportError:
        log.debug("dateparser not installed; falling through")
        return None

    phrase, remainder = _split_time_phrase(body)
    if phrase is None:
        return None
    dt = dateparser.parse(
        phrase,
        settings={
            "RELATIVE_BASE": now.astimezone().replace(tzinfo=None),
            "PREFER_DATES_FROM": "future",   # "at 4pm" means the next 4pm
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if dt is None:
        return None
    due = dt.astimezone(now.tzinfo or dt.tzinfo)
    if due <= now:
        # dateparser occasionally lands in the past on ambiguous input; a
        # reminder in the past is never what was meant.
        due += timedelta(days=1)
    return ParsedReminder(text=_clean(remainder) or _clean(body), kind="time",
                          due_at=due, urgent=bool(_URGENT.search(body)),
                          source="dateparser")


_TIME_PHRASE = re.compile(
    r"\b(?:"
    r"in\s+\d+\s*(?:min(?:ute)?s?|hours?|hrs?|days?|weeks?)"
    r"|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?"
    r"|(?:tomorrow|today|tonight)(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?"
    r"|next\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"|on\s+\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
    r")\b", re.I)


def _split_time_phrase(body: str) -> tuple[str | None, str]:
    """Separate the time expression from the thing to be reminded about, so the
    stored text reads 'email the supervisor', not 'email the supervisor at 4pm'."""
    m = _TIME_PHRASE.search(body)
    if not m:
        return None, body
    return m.group(0), (body[: m.start()] + " " + body[m.end():])


_LEADING_TO = re.compile(r"^to\s+", re.I)


def _clean(s: str) -> str:
    """Tidy the reminder body.

    Strips a stranded leading "to" (removing the time phrase from "remind me in
    20 minutes to check the run" otherwise leaves "to check the run") and the
    urgency word, which is captured as a flag and would otherwise be shown back
    to you inside the reminder body.
    """
    out = _URGENT.sub("", s or "")
    out = re.sub(r"\s+", " ", out).strip(" ,.;:-–—").strip()
    return _LEADING_TO.sub("", out).strip(" ,.;:-–—").strip()


# --- LLM fallback -----------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["time", "context", "unclear"]},
        "due_at": {"type": ["string", "null"],
                   "description": "ISO-8601 local datetime, or null"},
        "trigger_context": {"type": ["string", "null"],
                            "enum": [*CONTEXTS, None]},
        "text": {"type": "string", "description": "what to be reminded about"},
        "urgent": {"type": "boolean"},
    },
    "required": ["kind", "due_at", "trigger_context", "text", "urgent"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You convert a short reminder written in natural language into structured "
    "fields. Extract only what the text actually states. If the text does not "
    "state a time or a recognisable context trigger, return kind \"unclear\" "
    "with due_at and trigger_context null — never invent, infer or round a "
    "time that was not written. Return the reminder body in `text` with the "
    "time or trigger phrase removed."
)


def parse_with_llm(raw: str, llm, *, now=None) -> ParsedReminder:
    """Last resort before asking. Returns needs_confirmation on anything the
    model could not read, on a budget refusal, and on a malformed reply."""
    from ..llm import LLMError

    body = strip_prefix(raw)
    now = now or utcnow()
    prompt = (f"Current local time: {now.astimezone().isoformat(timespec='minutes')}\n"
              f"Reminder: {body}")
    try:
        result = llm.complete_json("reminder_parse", prompt, SCHEMA,
                                   cache_on=body.lower(), system=_SYSTEM)
    except LLMError as e:
        log.info("reminder LLM parse unavailable (%s); asking instead", e)
        return ParsedReminder(text=body, needs_confirmation=True, source="llm")

    data = result.data
    kind = data.get("kind")
    text = _clean(data.get("text") or body) or body
    urgent = bool(data.get("urgent"))

    if kind == "time" and data.get("due_at"):
        try:
            due = datetime.fromisoformat(str(data["due_at"]))
        except ValueError:
            return ParsedReminder(text=text, needs_confirmation=True, source="llm")
        if due.tzinfo is None:
            due = due.astimezone()
        return ParsedReminder(text=text, kind="time", due_at=due,
                              urgent=urgent, source="llm")

    if kind == "context" and valid_context(data.get("trigger_context")):
        return ParsedReminder(text=text, kind="context",
                              trigger_context=data["trigger_context"],
                              urgent=urgent, source="llm")

    return ParsedReminder(text=text, needs_confirmation=True, source="llm")


def parse(raw: str, llm=None, *, now=None) -> ParsedReminder:
    """The full ladder: rules, then dateparser, then the LLM, then ask."""
    return (parse_context(raw)
            or parse_time(raw, now=now)
            or (parse_with_llm(raw, llm, now=now) if llm is not None
                else ParsedReminder(text=strip_prefix(raw), needs_confirmation=True)))
