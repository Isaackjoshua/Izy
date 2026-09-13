"""Tiers 1 and 2 of the classification ladder: the free ones.

SPEC.md expects these to handle the majority of a day. Every event they settle
is an LLM call not made, so the rules are checked first and the paid tier only
ever sees what is genuinely ambiguous.

These are *absolute* judgements — "Spotify is never work" — unlike tier 3, which
asks whether something is plausibly related to the intent you declared. Deny
beats allow, so marking an app off-task is not undone by a matching title.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

#: Browsers whose window we should consult a tab URL for.
BROWSERS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "edge",
            "librewolf", "zen", "epiphany", "safari")


@dataclass(frozen=True)
class Decision:
    on_task: bool
    confidence: float
    reason: str
    tier: int
    #: Maps onto labels.source — 'rule' for tiers 1-2, 'llm' for 3, 'user' for 4.
    source: str = "rule"


def _matches(patterns, value: str | None) -> str | None:
    """Case-insensitive substring match, or regex when prefixed with 're:'.
    Returns the pattern that matched, for use in the reason string."""
    if not value:
        return None
    haystack = value.lower()
    for raw in patterns or ():
        pattern = str(raw)
        if pattern.startswith("re:"):
            try:
                if re.search(pattern[3:], value, re.I):
                    return pattern
            except re.error:
                continue          # a bad regex in config must not break tracking
        elif pattern.lower() in haystack:
            return pattern
    return None


def is_browser(app: str | None) -> bool:
    return bool(app) and any(b in app.lower() for b in BROWSERS)


def host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url if "://" in url else f"http://{url}").hostname
    except ValueError:
        return None
    return host.lower() if host else None


def tier1(cfg, app: str | None, title: str | None) -> Decision | None:
    """App and window title against the user's allow/deny lists. Free."""
    if hit := _matches(cfg.off_task_apps, app):
        return Decision(False, 1.0, f"app rule: {hit}", 1)
    if hit := _matches(cfg.off_task_titles, title):
        return Decision(False, 1.0, f"title rule: {hit}", 1)
    if hit := _matches(cfg.on_task_apps, app):
        return Decision(True, 1.0, f"app rule: {hit}", 1)
    if hit := _matches(cfg.on_task_titles, title):
        return Decision(True, 1.0, f"title rule: {hit}", 1)
    return None


def tier2(cfg, app: str | None, url: str | None) -> Decision | None:
    """Browser tab URL. Free, and resolves most of the YouTube/Twitter
    ambiguity a title alone cannot: 'Firefox' says nothing, the host says a lot.

    Only consulted for a browser window — a URL from a background tab is not
    what you are looking at.
    """
    if not url or not is_browser(app):
        return None
    host = host_of(url)
    if not host:
        return None
    if hit := _matches(cfg.off_task_urls, host):
        return Decision(False, 1.0, f"url rule: {hit}", 2)
    if hit := _matches(cfg.on_task_urls, host):
        return Decision(True, 1.0, f"url rule: {hit}", 2)
    return None


def free_tiers(cfg, app: str | None, title: str | None,
               url: str | None) -> Decision | None:
    """Tiers 1 and 2 in order. None means 'ask a paid tier'."""
    return tier1(cfg, app, title) or tier2(cfg, app, url)


def hint_decision(hints: dict | None, app: str | None, title: str | None,
                  url: str | None) -> Decision | None:
    """A task's hints resolve its own windows on-task for free (izy-v2.md §3).

    Only ever *on*-task and only at tier 1/2 — hints say "this task uses these
    apps and domains", never "this is off-task". Checked after the config deny
    rules (so a denied app still loses) but before any paid tier, which is what
    keeps a hinted app from ever costing a tier-3 call. The reason names the
    hint so the classification audit shows why it was free."""
    if not hints:
        return None
    apps = hints.get("apps") or ()
    domains = hints.get("domains") or ()
    keywords = hints.get("keywords") or ()
    if hit := _matches(apps, app):
        return Decision(True, 1.0, f"task hint: app {hit}", 1)
    if hit := _matches(keywords, title):
        return Decision(True, 1.0, f"task hint: keyword {hit}", 1)
    if url and (host := host_of(url)) and (hit := _matches(domains, host)):
        return Decision(True, 1.0, f"task hint: domain {hit}", 2)
    return None
