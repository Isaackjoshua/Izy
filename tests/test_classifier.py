"""The classification ladder.

SPEC.md's done-condition for Phase 3 is a cost one — a day of events classified
in fewer than 50 LLM calls — so the headline test here simulates a realistic day
and counts the calls. The rest pin the ladder's ordering and the rule that
matters most: on exceeding the budget, degrade to asking, never to a guess.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from izy import db, rules
from izy.classifier import Classifier, cache_key
from izy.llm import LLM, LLMBudgetExceeded
from izy.models import Snapshot, utcnow
from izy.tracker import Tracker

from .test_llm import FakeClient


# --- helpers ---------------------------------------------------------------

def _cfg(cfg, **classify):
    return replace(cfg, classify=replace(cfg.classify, **classify))


class VerdictClient(FakeClient):
    """Answers every batch on-task at high confidence unless told otherwise."""

    def __init__(self, confidence=0.95, on_task=True):
        super().__init__({"verdicts": []})
        self.confidence, self.on_task = confidence, on_task
        self.batch_sizes = []

    def _respond(self, prompt):
        ids = [int(m.group(1)) for m in
               (re.match(r"\s*\[(\d+)\]", line) for line in prompt.splitlines())
               if m]
        self.batch_sizes.append(len(ids))
        return {"verdicts": [{"id": i, "on_task": self.on_task,
                              "confidence": self.confidence,
                              "reason": "related to the intent"} for i in ids]}


class _Messages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        payload = self.outer._respond(kwargs["messages"][0]["content"])
        from .test_llm import FakeResponse
        return FakeResponse(payload)


def _client(confidence=0.95, on_task=True):
    c = VerdictClient(confidence, on_task)
    c.messages = _Messages(c)
    return c


def _classifier(conn, cfg, clock, client=None, **classify):
    cfg = _cfg(cfg, **classify) if classify else cfg
    llm = LLM(conn, cfg, client=client or _client(), clock=clock)
    return Classifier(conn, cfg, llm, clock=clock), llm


def _event(conn, clock, app, title, seconds=120, session_id=None, url=None):
    snap = Snapshot(ts=clock(), app=app, title=title, url=url)
    eid = db.open_event(conn, snap, session_id)
    db.update_event_duration(conn, eid, seconds)
    return conn.execute("SELECT * FROM activity_events WHERE id=?", (eid,)).fetchone()


# --- tiers 1 and 2 (free) --------------------------------------------------

def test_tier1_deny_beats_allow(cfg):
    c = replace(cfg.classify, on_task_apps=("code",), off_task_apps=("code",))
    assert rules.tier1(c, "code", "main.py").on_task is False


def test_tier1_matches_apps_and_titles(cfg):
    d = rules.tier1(cfg.classify, "Code", "main.py")
    assert d.on_task is True and d.tier == 1
    assert rules.tier1(cfg.classify, "Spotify", "some song").on_task is False
    assert rules.tier1(cfg.classify, "Unknown", "whatever") is None


def test_tier1_supports_regex_rules(cfg):
    c = replace(cfg.classify, off_task_titles=(r"re:^\[ad\]",))
    assert rules.tier1(c, "firefox", "[ad] buy things").on_task is False
    assert rules.tier1(c, "firefox", "not an ad") is None


def test_a_bad_regex_in_config_does_not_break_tracking(cfg):
    c = replace(cfg.classify, off_task_titles=("re:[unclosed",))
    assert rules.tier1(c, "firefox", "anything") is None


def test_tier2_resolves_what_a_title_cannot(cfg):
    """'Firefox' says nothing; the host says a lot."""
    assert rules.tier1(cfg.classify, "firefox", "Firefox") is None
    d = rules.tier2(cfg.classify, "firefox", "https://youtube.com/watch?v=1")
    assert d.on_task is False and d.tier == 2
    assert rules.tier2(cfg.classify, "firefox", "https://github.com/x/y").on_task


def test_tier2_ignores_urls_from_non_browsers(cfg):
    """A URL attached to a non-browser window is not what you are looking at."""
    assert rules.tier2(cfg.classify, "Code", "https://youtube.com/watch") is None


# --- the ladder ------------------------------------------------------------

def test_free_tiers_never_reach_the_paid_one(conn, cfg, clock):
    client = _client()
    c, llm = _classifier(conn, cfg, clock, client)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())

    for app, title in [("Code", "main.py"), ("Spotify", "a song"),
                       ("Code", "test_main.py")]:
        c.consider(_event(conn, clock, app, title, session_id=s.id), s.declared_intent)

    c.flush(force=True)
    assert client.calls == [], "tier 1 must settle these for free"
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 3


def test_short_spans_are_never_paid_for(conn, cfg, clock):
    client = _client()
    c, _ = _classifier(conn, cfg, clock, client, min_duration_s=20)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    c.consider(_event(conn, clock, "Unknown", "a glance", seconds=4,
                      session_id=s.id), s.declared_intent)
    c.flush(force=True)
    assert client.calls == []
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


def test_afk_is_not_off_task(conn, cfg, clock):
    c, _ = _classifier(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    snap = Snapshot(ts=clock(), app=None, title=None, afk=True)
    eid = db.open_event(conn, snap, s.id)
    db.update_event_duration(conn, eid, 600)
    row = conn.execute("SELECT * FROM activity_events WHERE id=?", (eid,)).fetchone()
    assert c.consider(row, s.declared_intent) is None
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


def test_ambiguous_events_are_batched_into_one_call(conn, cfg, clock):
    client = _client()
    c, _ = _classifier(conn, cfg, clock, client, batch_window_s=60)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())

    for i in range(5):
        c.consider(_event(conn, clock, "Obsidian", f"note {i}", session_id=s.id),
                   s.declared_intent)
    assert client.calls == [], "must wait for the batch window"

    clock.advance(seconds=61)
    decisions, ask = c.flush()
    assert len(client.calls) == 1, "five ambiguous events, one call"
    assert client.batch_sizes == [5]
    assert len(decisions) == 5 and ask == []


def test_identical_windows_never_cost_twice_in_a_session(conn, cfg, clock):
    """SPEC.md: cached on (intent_hash, app, normalized_title)."""
    client = _client()
    c, _ = _classifier(conn, cfg, clock, client)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())

    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)
    c.flush(force=True)
    assert len(client.calls) == 1

    for _ in range(5):
        c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
                   s.declared_intent)
        c.flush(force=True)
    assert len(client.calls) == 1, "identical titles must never trigger a second call"


def test_cache_key_ignores_volatile_title_noise():
    a = cache_key("fix the dataloader", "Code", "◑ main.py")
    b = cache_key("fix the dataloader", "Code", "◐ main.py")
    assert a == b
    assert a != cache_key("write the report", "Code", "main.py")


def test_no_intent_means_no_paid_call(conn, cfg, clock):
    """Tier 3's question has no meaning without a declared intent."""
    client = _client()
    c, _ = _classifier(conn, cfg, clock, client)
    c.consider(_event(conn, clock, "Obsidian", "notes"), None)
    c.flush(force=True)
    assert client.calls == []


def test_absolute_rules_still_apply_with_no_session(conn, cfg, clock):
    c, _ = _classifier(conn, cfg, clock)
    c.consider(_event(conn, clock, "Spotify", "a song"), None)
    row = conn.execute("SELECT * FROM labels").fetchone()
    assert row["on_task"] == 0 and row["source"] == "rule"


# --- tier 4 ----------------------------------------------------------------

def test_low_confidence_asks_instead_of_trusting(conn, cfg, clock):
    client = _client(confidence=0.4)
    c, _ = _classifier(conn, cfg, clock, client, confidence_threshold=0.7)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)

    decisions, ask = c.flush(force=True)
    assert decisions == [] and len(ask) == 1
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0, \
        "a low-confidence verdict must not be written as if it were known"


def test_budget_exhaustion_degrades_to_asking_never_to_a_guess(conn, cfg, clock):
    """The single most important rule in Feature 2."""
    client = _client()
    cfg2 = replace(cfg, llm=replace(cfg.llm, max_calls_per_day=0))
    llm = LLM(conn, cfg2, client=client, clock=clock)
    c = Classifier(conn, cfg2, llm, clock=clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)

    decisions, ask = c.flush(force=True)
    assert decisions == [] and len(ask) == 1
    assert client.calls == []
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


def test_no_llm_available_degrades_to_asking(conn, cfg, clock, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(conn, cfg, clock=clock)
    c = Classifier(conn, cfg, llm, clock=clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)
    decisions, ask = c.flush(force=True)
    assert decisions == [] and len(ask) == 1


def test_an_event_the_model_skipped_is_asked_about(conn, cfg, clock):
    """A missing verdict must never be read as 'probably fine'."""
    client = _client()
    client._respond = lambda prompt: {"verdicts": []}
    c, _ = _classifier(conn, cfg, clock, client)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)
    decisions, ask = c.flush(force=True)
    assert decisions == [] and len(ask) == 1


def test_user_answer_is_ground_truth_and_seeds_the_cache(conn, cfg, clock):
    client = _client()
    c, _ = _classifier(conn, cfg, clock, client)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    row = _event(conn, clock, "Obsidian", "notes", session_id=s.id)

    c.record_user_answer(row["id"], True)
    label = conn.execute("SELECT * FROM labels").fetchone()
    assert label["source"] == "user" and label["on_task"] == 1

    # The same window must now be free.
    c.consider(_event(conn, clock, "Obsidian", "notes", session_id=s.id),
               s.declared_intent)
    c.flush(force=True)
    assert client.calls == [], "a user answer must seed the cache"


def test_an_event_is_never_labelled_twice(conn, cfg, clock):
    c, _ = _classifier(conn, cfg, clock)
    s = db.start_session(conn, "fix the dataloader", 25, now=clock())
    row = _event(conn, clock, "Code", "main.py", session_id=s.id)
    c.consider(row, s.declared_intent)
    c.consider(row, s.declared_intent)
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 1


# --- the Phase 3 done-condition --------------------------------------------

def test_a_realistic_day_costs_fewer_than_fifty_calls(conn, cfg, clock):
    """SPEC.md Phase 3: 'a day of events is classified with fewer than 50 LLM
    calls'. Simulated here as a working day of spans across several sessions."""
    client = _client()
    c, llm = _classifier(conn, cfg, clock, client, batch_window_s=60)

    known = [("Code", "dataloader.py"), ("Code", "train.py"),
             ("gnome-terminal", "pytest"), ("Spotify", "a song")]
    ambiguous = [("Obsidian", "research notes"), ("Zoom", "standup"),
                 ("Nautilus", "Downloads"), ("Thunderbird", "inbox")]

    sessions = ["fix the dataloader", "write the report", "review the PR"]
    for intent in sessions:
        s = db.start_session(conn, intent, 50, now=clock())
        for hour in range(3):
            for app, title in known:
                c.consider(_event(conn, clock, app, title, session_id=s.id), intent)
            for app, title in ambiguous:
                c.consider(_event(conn, clock, app, title, session_id=s.id), intent)
            clock.advance(seconds=61)
            c.flush()
        c.flush(force=True)
        db.end_session(conn, s.id, "partly", now=clock())

    paid, spend = llm.spend_today()
    labels = conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
    assert labels >= 30, "a day's events should actually get labelled"
    assert paid < 50, f"{paid} paid calls for a day of events; budget is 50"
    # Repeats within a session are cached, so the real number is far lower.
    assert paid <= len(sessions), \
        f"expected roughly one call per session after caching, got {paid}"
