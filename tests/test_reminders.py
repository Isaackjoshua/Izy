"""Reminders: parsing, storage, and — the part that matters most — when they
are allowed to fire.

Nothing here calls the network. The LLM fallback is exercised through a stub.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from izy.reminders import ParsedReminder, ReminderScheduler, parse
from izy.reminders import store
from izy.reminders.parse import parse_context, parse_time, parse_with_llm
from izy.sessions import Phase

from .conftest import FakeClock


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("raw,context", [
    ("remind me next time I take a break to refill water", "on_break"),
    ("remind me on my next break to refill water", "on_break"),
    ("remind me when this session ends to push the branch", "session_end"),
    ("remind me at session start to open the ticket", "session_start"),
    ("remind me at the end of the day to write the standup note", "end_of_day"),
])
def test_context_triggers_parse_by_rule_for_free(raw, context):
    p = parse_context(raw)
    assert p is not None and p.kind == "context"
    assert p.trigger_context == context
    assert p.source == "rule"


def test_app_opened_trigger():
    p = parse_context("remind me next time I open slack to reply to the PR thread")
    assert p.trigger_context == "app_opened:slack"
    assert p.text == "reply to the PR thread"


def test_relative_and_absolute_times_parse_without_an_llm(clock):
    p = parse_time("remind me in 20 minutes to check the training run", now=clock())
    assert p.kind == "time" and p.source == "dateparser"
    assert p.text == "check the training run"
    assert 19 <= (p.due_at - clock()).total_seconds() / 60 <= 21


def test_the_time_phrase_is_removed_from_the_body(clock):
    p = parse_time("remind me to email the supervisor at 4pm", now=clock())
    assert p.text == "email the supervisor"


def test_urgency_is_a_flag_not_part_of_the_text(clock):
    p = parse_time("remind me urgently in 2 minutes to take the pizza out", now=clock())
    assert p.urgent is True
    assert p.text == "take the pizza out"


def test_a_due_time_is_never_in_the_past(clock):
    p = parse_time("remind me at 9am to stand up", now=clock())
    assert p.due_at > clock()


def test_unparseable_input_asks_rather_than_guessing(clock):
    """SPEC.md: never let anything invent a time that wasn't stated."""
    p = parse("remind me to do the thing", None, now=clock())
    assert p.needs_confirmation is True
    assert p.due_at is None and p.trigger_context is None
    assert p.is_valid() is False


# --- LLM fallback ----------------------------------------------------------

class StubLLM:
    def __init__(self, data=None, error=None):
        self.data, self.error = data, error
        self.calls = []

    def complete_json(self, purpose, prompt, schema, *, cache_on=None, system=None):
        self.calls.append((purpose, prompt, cache_on))
        if self.error:
            raise self.error
        from izy.llm import LLMResult
        return LLMResult(self.data)


def test_llm_is_only_consulted_after_the_free_paths(clock):
    llm = StubLLM({"kind": "time", "due_at": None, "trigger_context": None,
                   "text": "x", "urgent": False})
    parse("remind me in 20 minutes to check the run", llm, now=clock())
    parse("remind me on my next break to refill water", llm, now=clock())
    assert llm.calls == [], "dateparser and the rules must handle these for free"


def test_llm_fallback_produces_a_time(clock):
    due = (clock() + timedelta(hours=3)).astimezone()
    llm = StubLLM({"kind": "time", "due_at": due.isoformat(),
                   "trigger_context": None, "text": "call the vet", "urgent": False})
    p = parse_with_llm("remind me to call the vet after lunch", llm, now=clock())
    assert p.kind == "time" and p.source == "llm" and p.text == "call the vet"


def test_llm_saying_unclear_asks_rather_than_guessing(clock):
    llm = StubLLM({"kind": "unclear", "due_at": None, "trigger_context": None,
                   "text": "do the thing", "urgent": False})
    p = parse_with_llm("remind me to do the thing", llm, now=clock())
    assert p.needs_confirmation is True and p.is_valid() is False


def test_budget_refusal_degrades_to_asking(conn, cfg, clock):
    from izy.llm import LLMBudgetExceeded
    llm = StubLLM(error=LLMBudgetExceeded("daily budget spent"))
    p = parse_with_llm("remind me to do the thing", llm, now=clock())
    assert p.needs_confirmation is True, "a budget refusal must never become a guess"


def test_llm_rubbish_is_not_stored(clock):
    llm = StubLLM({"kind": "time", "due_at": "not-a-date",
                   "trigger_context": None, "text": "x", "urgent": False})
    assert parse_with_llm("remind me whenever", llm, now=clock()).needs_confirmation


# --- storage ---------------------------------------------------------------

def test_add_and_list_pending(conn, clock):
    store.add(conn, parse_time("remind me in 10 minutes to stretch", now=clock()),
              now=clock())
    store.add(conn, parse_context("remind me on my next break to refill water"),
              now=clock())
    items = store.pending(conn)
    assert {r.text for r in items} == {"stretch", "refill water"}


def test_an_unactionable_reminder_is_refused(conn, clock):
    """A row with neither a time nor a trigger would never fire and would rot."""
    with pytest.raises(ValueError):
        store.add(conn, ParsedReminder(text="do the thing", needs_confirmation=True))


def test_done_and_dismissed_leave_the_pending_list(conn, clock):
    r1 = store.add(conn, parse_time("remind me in 5 minutes to a", now=clock()), now=clock())
    r2 = store.add(conn, parse_time("remind me in 6 minutes to b", now=clock()), now=clock())
    store.mark_done(conn, r1.id)
    store.mark_dismissed(conn, r2.id)
    assert store.pending(conn) == []


def test_snoozing_a_context_reminder_makes_it_time_based(conn, clock):
    r = store.add(conn, parse_context("remind me on my next break to refill water"),
                  now=clock())
    due = store.snooze(conn, r.id, 10, now=clock())
    again = store.get(conn, r.id)
    assert again.status == "snoozed" and again.parsed_kind == "time"
    assert abs((due - clock()).total_seconds() - 600) < 2


# --- firing rules (the part that matters) ----------------------------------

def _sched(conn, cfg, clock, **overrides):
    if overrides:
        cfg = replace(cfg, reminders=replace(cfg.reminders, **overrides))
    return ReminderScheduler(conn, cfg, clock=clock)


def test_a_due_reminder_fires_when_idle(conn, cfg, clock):
    store.add(conn, parse_time("remind me in 5 minutes to stretch", now=clock()),
              now=clock())
    s = _sched(conn, cfg, clock)
    assert s.due_now(Phase.IDLE) == []
    clock.advance(minutes=6)
    assert [r.text for r in s.due_now(Phase.IDLE)] == ["stretch"]


def test_a_reminder_does_not_break_the_focus_it_protects(conn, cfg, clock):
    """SPEC.md: firing mid-session is a bug. It waits for the boundary."""
    store.add(conn, parse_time("remind me in 5 minutes to stretch", now=clock()),
              now=clock())
    s = _sched(conn, cfg, clock)
    clock.advance(minutes=6)

    assert s.due_now(Phase.FOCUS) == [], "must stay quiet during a focus session"
    assert [r.text for r in s.on_boundary(Phase.BREAK)] == ["stretch"]


def test_an_urgent_reminder_fires_mid_session(conn, cfg, clock):
    p = parse_time("remind me urgently in 5 minutes to take the pizza out", now=clock())
    store.add(conn, replace(p, text=f"[urgent] {p.text}"), now=clock())
    s = _sched(conn, cfg, clock)
    clock.advance(minutes=6)
    assert len(s.due_now(Phase.FOCUS)) == 1


def test_a_long_held_reminder_is_eventually_released(conn, cfg, clock):
    """Held forever is just as broken as fired immediately."""
    store.add(conn, parse_time("remind me in 1 minute to stretch", now=clock()),
              now=clock())
    s = _sched(conn, cfg, clock, max_defer_minutes=60)
    clock.advance(minutes=2)
    assert s.due_now(Phase.FOCUS) == []
    clock.advance(minutes=61)
    assert len(s.due_now(Phase.FOCUS)) == 1


def test_context_reminders_fire_at_their_trigger(conn, cfg, clock):
    store.add(conn, parse_context("remind me on my next break to refill water"),
              now=clock())
    s = _sched(conn, cfg, clock)
    assert s.on_context("session_start") == []
    assert [r.text for r in s.on_context("on_break")] == ["refill water"]


def test_phase_changes_map_to_the_right_contexts(conn, cfg, clock):
    s = _sched(conn, cfg, clock)
    assert s.contexts_for_phase_change(Phase.IDLE, Phase.FOCUS) == ["session_start"]
    assert s.contexts_for_phase_change(Phase.FOCUS, Phase.BREAK) == \
        ["session_end", "on_break"]
    assert s.contexts_for_phase_change(Phase.BREAK, Phase.IDLE) == []


def test_a_fired_reminder_does_not_fire_twice(conn, cfg, clock):
    r = store.add(conn, parse_time("remind me in 1 minute to stretch", now=clock()),
                  now=clock())
    s = _sched(conn, cfg, clock)
    clock.advance(minutes=2)
    assert len(s.due_now(Phase.IDLE)) == 1
    s.fired(r.id)
    assert s.due_now(Phase.IDLE) == []


def test_snoozed_reminder_comes_back(conn, cfg, clock):
    r = store.add(conn, parse_time("remind me in 1 minute to stretch", now=clock()),
                  now=clock())
    s = _sched(conn, cfg, clock, snooze_minutes=10)
    clock.advance(minutes=2)
    s.fired(r.id)
    s.snooze(r.id)
    assert s.due_now(Phase.IDLE) == []
    clock.advance(minutes=11)
    assert len(s.due_now(Phase.IDLE)) == 1


def test_end_of_day_fires_once_per_day(conn, cfg, clock):
    s = _sched(conn, cfg, clock, end_of_day_hour=0)  # always past the hour
    assert s.end_of_day_due(None) is True
    assert s.end_of_day_due(clock()) is False
    clock.advance(minutes=60 * 25)
    assert s.end_of_day_due(clock() - timedelta(days=1)) is True


def test_reminders_survive_a_restart(conn, cfg, clock):
    """Persisted in SQLite, so a new scheduler picks them up — SPEC.md's Phase 2
    done-condition includes firing correctly across a restart."""
    store.add(conn, parse_time("remind me in 5 minutes to stretch", now=clock()),
              now=clock())
    clock.advance(minutes=6)
    restarted = _sched(conn, cfg, clock)
    assert [r.text for r in restarted.due_now(Phase.IDLE)] == ["stretch"]
