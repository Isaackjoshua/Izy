"""Tasks, the Eisenhower matrix, and the hint integration (izy-v2.md §3).

The headline is the phase gate: a task with hints, a session started from it, and
the classification ladder short-circuiting at tier 1 for a hinted app while the
tier-3 counter does not move. The rest pin the derived quadrant, drag-to-set-flags,
reordering, and the suggestion loop that makes Izy cheaper the more a task is used.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from izy import db
from izy import tasks as T
from izy.classifier import Classifier
from izy.llm import LLM
from izy.models import Snapshot

from .test_classifier import _client   # a fake LLM that would answer if called


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


# --- the quadrant is derived ------------------------------------------------

@pytest.mark.parametrize("urgent,important,q", [
    (True, True, "Q1"), (False, True, "Q2"), (True, False, "Q3"), (False, False, "Q4"),
])
def test_quadrant_is_derived_from_flags(conn, urgent, important, q):
    t = T.create(conn, "a task", urgent=urgent, important=important, now=NOW)
    assert t.quadrant == q
    # nothing named "quadrant" is stored
    cols = {r[1] for r in conn.execute("PRAGMA table_info(task)")}
    assert "quadrant" not in cols


def test_dragging_between_quadrants_sets_the_flags(conn):
    t = T.create(conn, "a task", now=NOW)
    assert t.quadrant == "Q4"
    t = T.set_quadrant(conn, t.id, "Q1", now=NOW)
    assert t.urgent and t.important and t.quadrant == "Q1"
    t = T.set_quadrant(conn, t.id, "Q2", now=NOW)
    assert t.important and not t.urgent


# --- CRUD + ordering --------------------------------------------------------

def test_create_requires_a_title(conn):
    with pytest.raises(ValueError):
        T.create(conn, "   ", now=NOW)


def test_new_tasks_sort_to_the_top(conn):
    a = T.create(conn, "first", now=NOW)
    b = T.create(conn, "second", now=NOW)
    ids = [t.id for t in T.list_tasks(conn)]
    assert ids == [b.id, a.id], "the newest task sorts first"


def test_reorder_between_two_tasks(conn):
    a = T.create(conn, "a", now=NOW)
    b = T.create(conn, "b", now=NOW)
    c = T.create(conn, "c", now=NOW)
    # move a to sit between c and b (order is c, b, a)
    T.reorder(conn, a.id, after=c.id, before=b.id)
    assert [t.id for t in T.list_tasks(conn)] == [c.id, a.id, b.id]


def test_complete_sets_status_and_timestamp(conn):
    t = T.create(conn, "a task", now=NOW)
    t = T.complete(conn, t.id, now=NOW)
    assert t.status == "done" and t.completed_at is not None


def test_delete_cascades_to_children(conn):
    parent = T.create(conn, "parent", now=NOW)
    child = T.create(conn, "child", parent_id=parent.id, now=NOW)
    T.delete(conn, parent.id)
    assert T.get(conn, child.id) is None, "children go with the parent"


def test_children_are_listed_under_their_parent(conn):
    parent = T.create(conn, "parent", now=NOW)
    child = T.create(conn, "child", parent_id=parent.id, now=NOW)
    assert [t.id for t in T.list_tasks(conn)] == [parent.id], "top-level only by default"
    assert [t.id for t in T.children(conn, parent.id)] == [child.id]


# --- derived UI signals -----------------------------------------------------

def test_a_due_date_within_24h_looks_urgent_but_is_not_moved(conn):
    t = T.create(conn, "ship it", important=True, due_at=NOW + timedelta(hours=6),
                 now=NOW)
    assert T.looks_urgent(t, NOW) is True
    assert t.urgent is False, "a chip suggests; it never moves the flag itself"


def test_a_far_due_date_does_not_look_urgent(conn):
    t = T.create(conn, "later", due_at=NOW + timedelta(days=3), now=NOW)
    assert T.looks_urgent(t, NOW) is False


def test_old_q4_tasks_are_flagged_stale(conn):
    old = T.create(conn, "clutter", now=NOW - timedelta(days=20))
    assert T.is_stale_q4(old, NOW) is True
    fresh = T.create(conn, "recent", now=NOW - timedelta(days=1))
    assert T.is_stale_q4(fresh, NOW) is False


def test_neglected_q2_finds_an_untouched_important_task(conn):
    t = T.create(conn, "learn rust", important=True, now=NOW - timedelta(days=30))
    assert T.neglected_q2(conn, NOW).id == t.id
    # a session in the last week clears it
    db.start_session(conn, "learn rust", 25, now=NOW - timedelta(days=2))
    conn.execute("UPDATE sessions SET task_id = ? WHERE declared_intent = 'learn rust'",
                 (t.id,))
    assert T.neglected_q2(conn, NOW) is None


# --- hints (the phase gate) -------------------------------------------------

def _event(conn, clock, app, title, seconds, session_id, url=None):
    eid = db.open_event(conn, Snapshot(ts=clock(), app=app, title=title, url=url),
                        session_id)
    db.update_event_duration(conn, eid, seconds)
    return conn.execute("SELECT * FROM activity_events WHERE id=?", (eid,)).fetchone()


def test_a_hinted_app_short_circuits_at_tier_1_with_no_paid_call(conn, cfg, clock):
    """izy-v2.md §3 phase gate: a session from a task, a hinted app resolves at
    tier 1 for free, and the tier-3 counter does not move."""
    client = _client()
    llm = LLM(conn, cfg, client=client, clock=clock)
    classifier = Classifier(conn, cfg, llm, clock=clock)

    task = T.create(conn, "build the widget", hints={"apps": ["obsidian"]}, now=clock())
    s = db.start_session(conn, "build the widget", 60, now=clock(), task_id=task.id)
    classifier.set_session_hints(T.get_hints(conn, task.id))

    # Obsidian is neither in the config rules nor a browser — without the hint it
    # would go to the paid tier. With the hint it resolves on-task at tier 1.
    row = _event(conn, clock, "Obsidian", "widget notes", 120, s.id)
    decision = classifier.consider(row, "build the widget")
    classifier.flush(force=True)

    assert decision is not None and decision.on_task is True
    assert decision.tier == 1 and "task hint" in decision.reason
    assert client.calls == [], "a hinted app must never cost a tier-3 call"
    paid, _ = llm.spend_today()
    assert paid == 0, "the tier-3 counter did not move"


def test_hints_do_not_override_a_config_deny(conn, cfg, clock):
    """You would not hint Spotify, but if you did, the deny rule still wins."""
    from dataclasses import replace
    cfg = replace(cfg, classify=replace(cfg.classify, off_task_apps=("spotify",)))
    classifier = Classifier(conn, cfg, LLM(conn, cfg, client=_client(), clock=clock),
                            clock=clock)
    s = db.start_session(conn, "x", 60, now=clock())
    classifier.set_session_hints({"apps": ["spotify"]})
    row = _event(conn, clock, "Spotify", "a song", 120, s.id)
    decision = classifier.consider(row, "x")
    assert decision.on_task is False, "deny beats a hint"


def test_a_hinted_domain_resolves_at_tier_2(conn, cfg, clock):
    classifier = Classifier(conn, cfg, LLM(conn, cfg, client=_client(), clock=clock),
                            clock=clock)
    s = db.start_session(conn, "research", 60, now=clock())
    classifier.set_session_hints({"domains": ["notion.so"]})
    row = _event(conn, clock, "firefox", "workspace", 120, s.id,
                 url="https://notion.so/page")
    decision = classifier.consider(row, "research")
    assert decision.on_task and decision.tier == 2 and "domain" in decision.reason


def test_clearing_hints_restores_the_paid_path(conn, cfg, clock):
    classifier = Classifier(conn, cfg, LLM(conn, cfg, client=_client(), clock=clock),
                            clock=clock)
    s = db.start_session(conn, "x", 60, now=clock())
    classifier.set_session_hints({"apps": ["obsidian"]})
    assert classifier.consider(_event(conn, clock, "Obsidian", "a", 120, s.id), "x")
    classifier.set_session_hints(None)   # session ended
    # a fresh Obsidian span now has no hint and goes to the paid tier (queued)
    assert classifier.consider(_event(conn, clock, "Obsidian", "b", 120, s.id), "x") is None
    assert classifier._pending, "without the hint it queues for tier 3"


# --- suggestions ------------------------------------------------------------

def test_suggest_hint_records_a_candidate(conn):
    t = T.create(conn, "a task", now=NOW)
    T.suggest_hint(conn, t.id, "zed")
    assert T.get(conn, t.id).suggestions == ["zed"]


def test_suggest_hint_does_not_duplicate_or_re_suggest_a_known_hint(conn):
    t = T.create(conn, "a task", hints={"apps": ["code"]}, now=NOW)
    T.suggest_hint(conn, t.id, "code")     # already a hint
    T.suggest_hint(conn, t.id, "zed")
    T.suggest_hint(conn, t.id, "zed")      # dup
    assert T.get(conn, t.id).suggestions == ["zed"]


def test_accepting_a_hint_moves_it_from_suggestions(conn):
    t = T.create(conn, "a task", now=NOW)
    T.suggest_hint(conn, t.id, "zed")
    t = T.add_hint(conn, t.id, app="zed", now=NOW)
    assert "zed" in t.hints["apps"]
    assert t.suggestions == [], "an accepted suggestion is consumed"
