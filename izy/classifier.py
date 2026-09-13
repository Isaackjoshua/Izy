"""The classification ladder.

Evaluate in order, stop at the first confident answer (SPEC.md Feature 2):

    tier 1  app + title vs the user's rules            free
    tier 2  browser tab URL                            free
    tier 3  one LLM call, batched, cached              paid
    tier 4  ask, one tap                               free

Cost discipline is a hard requirement, not an optimisation, so this module is
built to *avoid* tier 3 rather than to use it well:

  * spans under `min_duration_s` are never judged at all;
  * an event with no declared intent is skipped, because the question tier 3
    asks is "is this related to what you said you were doing", which has no
    meaning without a session;
  * ambiguous events are buffered and judged several per call;
  * the cache is keyed on (intent, app, normalized_title) exactly as SPEC.md
    specifies, so the same window in the same session is never paid for twice;
  * on exceeding the budget it degrades to tier 4 — asking — and *never*
    silently to a guess.

Everything here is pure except the SQLite writes, so a day of classification is
replayable in a test in milliseconds.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from . import db, rules
from .llm import LLMError
from .models import to_iso, utcnow
from .rules import Decision
from .titles import normalize_title

log = logging.getLogger(__name__)

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "on_task": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "on_task", "confidence", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You judge whether a window someone had focused is plausibly related to the "
    "task they said they were working on. You are not judging whether it is "
    "productive in the abstract — reading documentation, searching for an error "
    "message, or checking a dependency's repository are all on-task for a "
    "programming task. Give a confidence between 0 and 1, and be honest when a "
    "window is genuinely ambiguous: a low confidence is useful, a confident "
    "guess is not. Keep each reason to one short clause."
)


@dataclass(frozen=True)
class Pending:
    """An event waiting for a paid verdict."""
    event_id: int
    app: str | None
    title: str | None
    url: str | None
    intent: str


def intent_hash(intent: str) -> str:
    return hashlib.sha256((intent or "").strip().lower().encode()).hexdigest()[:16]


def cache_key(intent: str, app: str | None, title: str | None) -> str:
    """SPEC.md: keyed on (intent_hash, app, normalized_title)."""
    return f"{intent_hash(intent)}|{(app or '').lower()}|{normalize_title(title) or ''}"


class Classifier:
    def __init__(self, conn, cfg, llm, *, clock=utcnow) -> None:
        self.conn = conn
        self.cfg = cfg
        self.llm = llm
        self.clock = clock
        self._pending: list[Pending] = []
        self._batch_opened_at = None
        #: event ids we have already asked the user about, so tier 4 does not
        #: re-ask about the same window every tick.
        self._asked: set[int] = set()
        #: hints of the task backing the current session, or None. Loaded when a
        #: task-backed session starts (izy-v2.md §3), so the task's own apps and
        #: domains resolve on-task for free and never reach the paid tier.
        self._session_hints: dict | None = None

    def set_session_hints(self, hints: dict | None) -> None:
        self._session_hints = hints or None

    # --- entry point -------------------------------------------------------

    def consider(self, event_row, intent: str | None) -> Decision | None:
        """Judge one finished activity span.

        Returns a Decision when tiers 1-2 settled it, None when the event was
        skipped or has been queued for a paid verdict.
        """
        if not self.cfg.classify.enabled:
            return None
        if event_row["afk"]:
            return None                       # away is not off-task
        if (event_row["duration_s"] or 0) < self.cfg.classify.min_duration_s:
            return None
        if not intent:
            # No declared intent means tier 3's question is meaningless. Tiers
            # 1-2 are still absolute, so they can still settle it for free.
            decision = rules.free_tiers(self.cfg.classify, event_row["app"],
                                        event_row["window_title"], event_row["url"])
            if decision:
                self._record(event_row["id"], decision)
            return decision
        if self._already_labelled(event_row["id"]):
            return None

        decision = rules.free_tiers(self.cfg.classify, event_row["app"],
                                    event_row["window_title"], event_row["url"])
        if decision:
            self._record(event_row["id"], decision)
            return decision

        # Task hints: the current task's own apps/domains are on-task for free,
        # checked after the config deny rules but before any paid tier.
        hint = rules.hint_decision(self._session_hints, event_row["app"],
                                   event_row["window_title"], event_row["url"])
        if hint:
            self._record(event_row["id"], hint)
            return hint

        cached = self._cached_verdict(intent, event_row)
        if cached:
            self._record(event_row["id"], cached)
            return cached

        self._enqueue(event_row, intent)
        return None

    # --- tier 3 ------------------------------------------------------------

    def _enqueue(self, event_row, intent: str) -> None:
        if any(p.event_id == event_row["id"] for p in self._pending):
            return
        if self._batch_opened_at is None:
            self._batch_opened_at = self.clock()
        self._pending.append(Pending(
            event_row["id"], event_row["app"], event_row["window_title"],
            event_row["url"], intent))

    def batch_ready(self) -> bool:
        if not self._pending:
            return False
        waited = (self.clock() - self._batch_opened_at).total_seconds()
        return waited >= self.cfg.classify.batch_window_s

    def flush(self, *, force: bool = False) -> tuple[list[Decision], list[Pending]]:
        """Judge the buffered events in one call.

        Returns (decisions, needs_asking). Anything the LLM could not answer
        confidently, plus everything left unjudged by a budget refusal or an
        error, comes back as needs_asking — tier 4. Never a guess.
        """
        if not self._pending or (not force and not self.batch_ready()):
            return [], []

        batch, self._pending, self._batch_opened_at = self._pending, [], None

        if not self.llm.available():
            log.info("no LLM available; %d event(s) go to tier 4", len(batch))
            return [], self._to_ask(batch)

        try:
            result = self.llm.complete_json(
                "classify", _render(batch), BATCH_SCHEMA, system=_SYSTEM,
                cache_on=_batch_cache_key(batch))
        except LLMError as e:
            log.info("tier 3 unavailable (%s); %d event(s) go to tier 4",
                     e, len(batch))
            return [], self._to_ask(batch)

        by_id = {p.event_id: p for p in batch}
        decisions, ask = [], []
        answered = set()
        for verdict in result.data.get("verdicts", []):
            pending = by_id.get(verdict.get("id"))
            if pending is None:
                continue
            answered.add(pending.event_id)
            confidence = float(verdict.get("confidence") or 0.0)
            decision = Decision(bool(verdict.get("on_task")), confidence,
                                str(verdict.get("reason") or "")[:200], 3, "llm")
            if confidence < self.cfg.classify.confidence_threshold:
                # Below the threshold the model is guessing. Ask instead.
                ask.append(pending)
                continue
            self._record(pending.event_id, decision)
            self._cache_verdict(pending, decision)
            decisions.append(decision)

        # Anything the model silently dropped is asked about, not assumed.
        ask.extend(p for p in batch if p.event_id not in answered)
        return decisions, self._to_ask(ask)

    def _to_ask(self, pendings) -> list[Pending]:
        out = [p for p in pendings if p.event_id not in self._asked]
        self._asked.update(p.event_id for p in out)
        return out

    # --- tier 4 ------------------------------------------------------------

    def record_user_answer(self, event_id: int, on_task: bool) -> None:
        """Ground truth. Also seeds the cache, so the same window in this
        session never costs a call again."""
        self._record(event_id, Decision(on_task, 1.0, "you said so", 4, "user"))
        row = self.conn.execute(
            "SELECT e.app, e.window_title, s.declared_intent FROM activity_events e"
            " LEFT JOIN sessions s ON s.id = e.session_id WHERE e.id = ?",
            (event_id,)).fetchone()
        if row and row["declared_intent"]:
            self._write_cache(
                cache_key(row["declared_intent"], row["app"], row["window_title"]),
                on_task, 1.0, "you said so")

    # --- persistence -------------------------------------------------------

    def _record(self, event_id: int, decision: Decision) -> int:
        return db.add_label(self.conn, event_id, decision.source, decision.on_task,
                            confidence=decision.confidence, reason=decision.reason)

    def _already_labelled(self, event_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM labels WHERE event_id = ? LIMIT 1", (event_id,)
        ).fetchone() is not None

    def _cached_verdict(self, intent: str, event_row) -> Decision | None:
        row = self.conn.execute(
            "SELECT response FROM llm_cache WHERE key = ?",
            (self._store_key(intent, event_row["app"], event_row["window_title"]),),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["response"])
        except ValueError:
            return None
        return Decision(bool(data["on_task"]), float(data.get("confidence", 1.0)),
                        data.get("reason", "cached"), 3, data.get("source", "llm"))

    def _cache_verdict(self, pending: Pending, decision: Decision) -> None:
        self._write_cache(cache_key(pending.intent, pending.app, pending.title),
                          decision.on_task, decision.confidence, decision.reason)

    def _write_cache(self, key: str, on_task: bool, confidence: float,
                     reason: str) -> None:
        self.conn.execute(
            "INSERT INTO llm_cache(key, purpose, response, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET response=excluded.response",
            (f"classify:{key}", "classify",
             json.dumps({"on_task": on_task, "confidence": confidence,
                         "reason": reason, "source": "llm"}),
             to_iso(self.clock())))

    @staticmethod
    def _store_key(intent, app, title) -> str:
        return f"classify:{cache_key(intent, app, title)}"


def _render(batch: list[Pending]) -> str:
    lines = [f"Declared intent: {batch[0].intent}", "",
             "Judge each window against that intent:"]
    for p in batch:
        parts = [f"[{p.event_id}] app={p.app or '?'}",
                 f"title={normalize_title(p.title) or '?'}"]
        if p.url:
            parts.append(f"url={p.url}")
        lines.append("  " + "  ".join(parts))
    return "\n".join(lines)


def _batch_cache_key(batch: list[Pending]) -> str:
    """Key the whole batch so an identical set of windows is free the second
    time, independent of the event ids assigned that day."""
    inner = "|".join(sorted(cache_key(p.intent, p.app, p.title) for p in batch))
    return hashlib.sha256(inner.encode()).hexdigest()[:32]
