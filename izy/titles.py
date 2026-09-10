"""Window-title normalisation.

Many titles carry a component that changes on a timer while the activity does
not: a spinner frame, a notification count, a media position, a percentage.
Left alone these churn the span key, so a single continuous activity is written
as one row per poll — measured in production at 1-second spans, which would be
~86k rows a day and a retrospective timeline nobody can read.

Normalising is deliberately conservative. Each rule targets a component that is
volatile *by construction*; anything that might carry meaning is left alone. The
normalised form is what gets stored and compared, because the exact spinner
frame at the moment of sampling carries no information worth keeping.

SPEC.md's Tier 3 cache key is `(intent_hash, app, normalized_title)`, so this is
also the function Phase 3 needs.
"""
from __future__ import annotations

import re

#: Spinner frames: braille, circles, quadrants, blocks, arrows, clock faces.
_SPINNER_CHARS = (
    "⠀-⣿"      # braille (⠋⠙⠹…)
    "◐-◓"      # ◐◑◒◓
    "◴-◷"      # ◴◵◶◷
    "▖-▟"      # quadrants
    "▁-█"      # ▁▂▃▄▅▆▇█
    "←-↻"      # arrows
    "\U0001f550-\U0001f567"  # clock faces
)

_RULES: list[tuple[re.Pattern, str]] = [
    # Leading spinner glyph(s), with or without trailing space: "◑ Building…"
    (re.compile(rf"^[{_SPINNER_CHARS}|/\\\-]+\s*"), ""),
    # Trailing spinner glyph(s): "Building… ⠹"
    (re.compile(rf"\s*[{_SPINNER_CHARS}]+$"), ""),
    # Notification counters: "(3) Inbox", "[12] Slack"
    (re.compile(r"^[(\[]\d+[)\]]\s*"), ""),
    # Media / elapsed position anywhere: "1:23 / 4:56", "01:02:03"
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*/\s*\d{1,2}:\d{2}(?::\d{2})?)?"), ""),
    # Progress percentages: "45%", "7.5 %"
    (re.compile(r"\b\d{1,3}(?:\.\d+)?\s*%"), ""),
]

_WS = re.compile(r"\s+")
#: Separators left adjacent once the volatile chunk between them is removed,
#: e.g. "Song - 1:23 / 4:56 - Spotify" would otherwise leave "Song - - Spotify".
_INNER_DUP = re.compile(r"\s*([\-–—|:•·])\s*(?:[\-–—|:•·]\s*)+")
#: Separators left stranded once a volatile chunk between them is removed.
_STRANDED = re.compile(r"^[\s\-–—|:•·,]+|[\s\-–—|:•·,]+$")


def normalize_title(title: str | None) -> str | None:
    """Strip the parts of a window title that change on a timer.

    Returns None for None, and preserves the original if normalising would
    empty it — a title made entirely of a spinner is still better than nothing
    for telling two activities apart.
    """
    if not title:
        return title
    out = title
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    out = _INNER_DUP.sub(r" \1 ", _WS.sub(" ", out))
    out = _STRANDED.sub("", out).strip()
    return out or title.strip()
