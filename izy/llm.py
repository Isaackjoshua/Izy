"""The single gateway for every LLM call in Izy.

SPEC.md is explicit: all LLM calls go through this module, which enforces
caching, rate limiting and a hard daily call budget, and no LLM call may be
made from anywhere else in the codebase. `tests/test_llm.py` asserts that with
a source scan, so the rule is checked rather than merely stated.

Cost discipline is a hard requirement, not an optimisation, so the order here is
always: cache -> budget -> call. A budget refusal raises `LLMBudgetExceeded` and
the caller degrades to asking the user; it never silently guesses.

Nothing here runs at import time, and with no API key the module still loads and
`available()` returns False — the whole test suite runs offline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from .models import from_iso, to_iso, utcnow

log = logging.getLogger(__name__)

#: USD per million tokens, from the Anthropic pricing table.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
_DEFAULT_PRICE = (5.00, 25.00)


class LLMError(Exception):
    """Base for every failure this module raises."""


class LLMUnavailable(LLMError):
    """No API key, or the SDK is not installed. Expected offline; not a bug."""


class LLMBudgetExceeded(LLMError):
    """The hourly or daily call budget is spent. Degrade to asking the user."""


@dataclass(frozen=True)
class LLMResult:
    data: dict
    cached: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


def _cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = PRICING.get(model, _DEFAULT_PRICE)
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


class LLM:
    def __init__(self, conn, cfg, *, client=None, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self._client = client          # injected in tests; never touches the network
        self._client_tried = client is not None

    # --- availability ------------------------------------------------------

    def available(self) -> bool:
        if not self.cfg.llm.enabled:
            return False
        try:
            return self._get_client() is not None
        except LLMUnavailable:
            return False

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._client_tried:
            raise LLMUnavailable("no client")
        self._client_tried = True
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as e:
            raise LLMUnavailable("the anthropic package is not installed") from e
        self._client = anthropic.Anthropic()
        return self._client

    # --- budget ------------------------------------------------------------

    def _calls_since(self, delta: timedelta) -> int:
        """Paid calls only — a cache hit costs nothing and must not be rationed."""
        since = to_iso(self.clock() - delta)
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND cached = 0", (since,)
        ).fetchone()[0]

    def _calls_today(self) -> int:
        start = self.clock().astimezone().replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
        return self.conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND cached = 0",
            (to_iso(start),),
        ).fetchone()[0]

    def check_budget(self) -> tuple[bool, str]:
        """(allowed, reason). Reason is returned either way so a refusal is
        explainable rather than mysterious."""
        hourly = self._calls_since(timedelta(hours=1))
        if hourly >= self.cfg.llm.max_calls_per_hour:
            return False, f"hourly LLM budget spent ({hourly}/{self.cfg.llm.max_calls_per_hour})"
        daily = self._calls_today()
        if daily >= self.cfg.llm.max_calls_per_day:
            return False, f"daily LLM budget spent ({daily}/{self.cfg.llm.max_calls_per_day})"
        return True, f"allowed ({daily}/{self.cfg.llm.max_calls_per_day} today)"

    # --- cache -------------------------------------------------------------

    @staticmethod
    def cache_key(purpose: str, model: str, payload: str) -> str:
        h = hashlib.sha256(f"{purpose}\x00{model}\x00{payload}".encode()).hexdigest()
        return h[:32]

    def _cache_get(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT response FROM llm_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["response"])
        except ValueError:
            return None

    def _cache_put(self, key: str, purpose: str, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO llm_cache(key, purpose, response, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET response=excluded.response",
            (key, purpose, json.dumps(data), to_iso(self.clock())),
        )

    # --- the one call path -------------------------------------------------

    def complete_json(self, purpose: str, prompt: str, schema: dict, *,
                      cache_on: str | None = None, system: str | None = None) -> LLMResult:
        """Ask for one JSON object matching `schema`.

        `cache_on` is the value the cache keys on when it should be narrower
        than the full prompt — Tier 3 keys on (intent, app, normalized_title)
        rather than the rendered prompt, so identical windows never pay twice.
        """
        model = self.cfg.llm.model
        key = self.cache_key(purpose, model, cache_on if cache_on is not None else prompt)

        hit = self._cache_get(key)
        if hit is not None:
            self._log(purpose, model, 0, 0, 0.0, cached=True)
            return LLMResult(hit, cached=True)

        allowed, reason = self.check_budget()
        if not allowed:
            log.warning("LLM call refused: %s", reason)
            raise LLMBudgetExceeded(reason)

        client = self._get_client()
        try:
            response = client.messages.create(
                model=model,
                max_tokens=self.cfg.llm.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": self.cfg.llm.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except LLMError:
            raise
        except Exception as e:
            self._log(purpose, model, 0, 0, 0.0, ok=False, error=repr(e)[:300])
            raise LLMError(f"{purpose}: {e}") from e

        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        cost = _cost(model, in_tok, out_tok)

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        try:
            data = json.loads(text)
        except ValueError as e:
            self._log(purpose, model, in_tok, out_tok, cost, ok=False,
                      error=f"non-JSON response: {text[:120]}")
            raise LLMError(f"{purpose}: model did not return JSON") from e

        self._cache_put(key, purpose, data)
        self._log(purpose, model, in_tok, out_tok, cost)
        return LLMResult(data, False, in_tok, out_tok, cost)

    # --- accounting --------------------------------------------------------

    def _log(self, purpose: str, model: str, in_tok: int, out_tok: int,
             cost: float, *, cached: bool = False, ok: bool = True,
             error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO llm_calls(ts, purpose, model, input_tokens, output_tokens,"
            " cost_usd, cached, ok, error) VALUES (?,?,?,?,?,?,?,?,?)",
            (to_iso(self.clock()), purpose, model, in_tok, out_tok, cost,
             int(cached), int(ok), error),
        )

    def spend_today(self) -> tuple[int, float]:
        """(paid calls, USD) so far today — what the dashboard shows."""
        start = self.clock().astimezone().replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM llm_calls"
            " WHERE ts >= ? AND cached = 0", (to_iso(start),)).fetchone()
        return int(row[0]), float(row[1])
