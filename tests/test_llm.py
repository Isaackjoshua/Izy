"""The LLM gateway.

SPEC.md requires that every LLM call path has a test that runs with the API
mocked, and that the whole suite runs offline with no key set. Nothing in this
file touches the network: the fake client below is the only "API" involved.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import replace

import pytest

from izy.llm import LLM, LLMBudgetExceeded, LLMError, LLMUnavailable

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}},
          "required": ["x"], "additionalProperties": False}


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeUsage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class FakeResponse:
    def __init__(self, payload, i=100, o=20):
        self.content = [FakeBlock(json.dumps(payload))]
        self.usage = FakeUsage(i, o)


class FakeMessages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        if self.outer.raises:
            raise self.outer.raises
        return self.outer.response


class FakeClient:
    """Stands in for anthropic.Anthropic. Never opens a socket."""

    def __init__(self, payload=None, raises=None):
        self.response = FakeResponse(payload if payload is not None else {"x": "ok"})
        self.raises = raises
        self.calls = []
        self.messages = FakeMessages(self)


def _llm(conn, cfg, clock, client=None, **llm_overrides):
    if llm_overrides:
        cfg = replace(cfg, llm=replace(cfg.llm, **llm_overrides))
    return LLM(conn, cfg, client=client or FakeClient(), clock=clock)


# --- the enforcement SPEC.md actually asks for -----------------------------

def test_no_llm_call_lives_outside_llm_py():
    """SPEC.md: 'No LLM call may be made from anywhere else in the codebase.'
    Checked, not merely stated."""
    root = pathlib.Path(__file__).resolve().parent.parent / "izy"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "llm.py":
            continue
        src = path.read_text()
        if re.search(r"\banthropic\b|messages\s*\.\s*create\s*\(", src):
            offenders.append(path.name)
    assert offenders == [], f"LLM access outside llm.py: {offenders}"


def test_module_imports_and_reports_unavailable_with_no_key(conn, cfg, clock, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    llm = LLM(conn, cfg, clock=clock)
    assert llm.available() is False
    with pytest.raises(LLMUnavailable):
        llm.complete_json("p", "prompt", SCHEMA)


def test_disabled_in_config_means_unavailable(conn, cfg, clock):
    llm = _llm(conn, cfg, clock, enabled=False)
    assert llm.available() is False


# --- caching ---------------------------------------------------------------

def test_identical_questions_are_paid_for_once(conn, cfg, clock):
    client = FakeClient({"x": "one"})
    llm = _llm(conn, cfg, clock, client=client)

    first = llm.complete_json("p", "same prompt", SCHEMA)
    second = llm.complete_json("p", "same prompt", SCHEMA)

    assert len(client.calls) == 1, "second identical call must come from cache"
    assert first.cached is False and second.cached is True
    assert second.data == {"x": "one"}


def test_cache_on_narrows_the_key(conn, cfg, clock):
    """Tier 3 keys on (intent, app, title), not the rendered prompt — a clock in
    the prompt must not defeat the cache."""
    client = FakeClient()
    llm = _llm(conn, cfg, clock, client=client)
    llm.complete_json("p", "prompt at 10:00", SCHEMA, cache_on="stable-key")
    llm.complete_json("p", "prompt at 10:05", SCHEMA, cache_on="stable-key")
    assert len(client.calls) == 1


def test_cache_hits_are_free_and_not_rationed(conn, cfg, clock):
    client = FakeClient()
    llm = _llm(conn, cfg, clock, client=client, max_calls_per_day=1)
    llm.complete_json("p", "q", SCHEMA)
    for _ in range(10):
        assert llm.complete_json("p", "q", SCHEMA).cached is True
    assert llm.spend_today()[0] == 1


# --- budget ----------------------------------------------------------------

def test_daily_budget_is_a_hard_stop(conn, cfg, clock):
    client = FakeClient()
    llm = _llm(conn, cfg, clock, client=client, max_calls_per_day=2)
    llm.complete_json("p", "a", SCHEMA)
    llm.complete_json("p", "b", SCHEMA)
    with pytest.raises(LLMBudgetExceeded):
        llm.complete_json("p", "c", SCHEMA)
    assert len(client.calls) == 2


def test_hourly_budget_is_a_hard_stop_and_frees_up(conn, cfg, clock):
    llm = _llm(conn, cfg, clock, max_calls_per_hour=1, max_calls_per_day=100)
    llm.complete_json("p", "a", SCHEMA)
    with pytest.raises(LLMBudgetExceeded):
        llm.complete_json("p", "b", SCHEMA)
    clock.advance(minutes=61)
    llm.complete_json("p", "c", SCHEMA)


def test_budget_survives_a_restart(conn, cfg, clock):
    """Counted from llm_calls, not memory."""
    first = _llm(conn, cfg, clock, max_calls_per_day=1)
    first.complete_json("p", "a", SCHEMA)
    restarted = _llm(conn, cfg, clock, max_calls_per_day=1)
    with pytest.raises(LLMBudgetExceeded):
        restarted.complete_json("p", "b", SCHEMA)


# --- accounting ------------------------------------------------------------

def test_every_call_is_logged_with_tokens_and_cost(conn, cfg, clock):
    llm = _llm(conn, cfg, clock, model="claude-opus-5")
    result = llm.complete_json("reminder_parse", "q", SCHEMA)

    row = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert row["purpose"] == "reminder_parse"
    assert row["input_tokens"] == 100 and row["output_tokens"] == 20
    # opus-5: $5/1M in, $25/1M out
    assert row["cost_usd"] == pytest.approx(100 * 5 / 1e6 + 20 * 25 / 1e6)
    assert result.cost_usd == pytest.approx(row["cost_usd"])

    calls, spend = llm.spend_today()
    assert calls == 1 and spend > 0


def test_api_failures_are_logged_then_raised(conn, cfg, clock):
    client = FakeClient(raises=RuntimeError("connection reset"))
    llm = _llm(conn, cfg, clock, client=client)
    with pytest.raises(LLMError):
        llm.complete_json("p", "q", SCHEMA)
    row = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert row["ok"] == 0 and "connection reset" in row["error"]


def test_non_json_response_is_an_error_not_a_guess(conn, cfg, clock):
    client = FakeClient()
    client.response.content = [FakeBlock("I'm not JSON")]
    llm = _llm(conn, cfg, clock, client=client)
    with pytest.raises(LLMError):
        llm.complete_json("p", "q", SCHEMA)
    assert conn.execute("SELECT ok FROM llm_calls").fetchone()[0] == 0


def test_request_uses_structured_output_and_configured_effort(conn, cfg, clock):
    client = FakeClient()
    llm = _llm(conn, cfg, clock, client=client, effort="low", model="claude-opus-5")
    llm.complete_json("p", "q", SCHEMA, system="be terse")

    sent = client.calls[0]
    assert sent["model"] == "claude-opus-5"
    assert sent["system"] == "be terse"
    assert sent["output_config"]["effort"] == "low"
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
