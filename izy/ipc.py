"""The control-plane boundary between the tick and everything that talks to it.

Izy v2 turns the daemon into a server: a dashboard, a widget and the CLI all
need to read state and ask for changes. The invariant from izy-v2.md §1 is that
there is exactly **one owner of the tick loop and the DB writer** — so nothing
outside the tick may write to SQLite or mutate pipeline state directly.

Two objects enforce that, and both are plain Python with no Qt and no FastAPI in
them, so the tick stays unit-testable with a fake clock:

  * `CommandQueue` — anyone (an API handler on another thread, a test) submits a
    `Command`; the tick drains it at step 1 and runs it *on the tick thread*.
    Every other writer becomes a request. A `Future` carries the result back so
    an HTTP POST can feel synchronous without a second writer ever existing.

  * `StateBus` — the tick's final step publishes an immutable `StateSnapshot`.
    Readers get the latest at any time; async subscribers get every new one,
    delivered onto their own event loop. This is what `/state` and `/events`
    serve, and it is the only thing the mascot, the dashboard and the widget
    ever read, so they cannot disagree.

Cross-thread delivery is explicit: `publish()` runs on the tick thread, and each
subscriber hands the bus its asyncio loop so delivery hops threads via
`loop.call_soon_threadsafe`. Nothing here assumes an event loop is running,
which is why a test can `publish()` and read `latest()` with no loop at all.
"""
from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field, asdict
from typing import Any

log = logging.getLogger(__name__)


# --- commands ---------------------------------------------------------------

@dataclass
class Command:
    """A request for the tick to perform a mutation, with a Future for its result.

    `name` maps to a Pipeline method the tick dispatches (see
    `Pipeline._COMMANDS`). Never construct one to call anything else — the whole
    point is that mutations funnel through the single writer.
    """
    name: str
    args: dict = field(default_factory=dict)
    future: Future = field(default_factory=Future)


class CommandQueue:
    """Thread-safe queue of pending commands. Submit from any thread; drain only
    from the tick thread."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Command]" = queue.Queue()

    def submit(self, name: str, /, **args) -> Future:
        """Enqueue a command and return a Future the tick will complete. The
        caller may wait on it (with a timeout) or ignore it."""
        cmd = Command(name, args)
        self._q.put(cmd)
        return cmd.future

    def drain(self) -> list[Command]:
        """Pop everything queued so far. Non-blocking; returns [] when empty."""
        out: list[Command] = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out

    def pending(self) -> int:
        return self._q.qsize()


# --- state snapshot ---------------------------------------------------------

@dataclass(frozen=True)
class StateSnapshot:
    """An immutable picture of the daemon at the end of one tick.

    Everything a client renders comes from here. Frozen so a subscriber can hold
    a reference without the next tick mutating it under them, and JSON-friendly
    so the API can return it verbatim.
    """
    tick: int = 0
    ts: str = ""                       # ISO-8601 UTC of this tick
    mascot: str = "asleep"             # asleep | neutral | soft-alert | resting
    phase: str = "idle"                # idle | focus | break
    watcher: str = ""                  # human description of the activity source
    session: dict | None = None        # {id, intent, planned_minutes, elapsed_s, remaining_s}
    focus_app: str | None = None       # what the watcher last saw focused
    focus_title: str | None = None
    counters: dict = field(default_factory=dict)   # today's on/off/afk seconds, sessions, tier3 calls
    connected: bool = True             # the daemon is alive (clients flip this false locally)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateBus:
    """Holds the latest snapshot and fans new ones out to async subscribers.

    Publish from the tick thread; subscribe from an asyncio thread. Delivery
    across the boundary is done by scheduling `put_nowait` onto the subscriber's
    own loop, so a slow or absent consumer never blocks the tick.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: StateSnapshot | None = None
        # subscriber id -> (asyncio.Queue, asyncio loop)
        self._subs: dict[int, tuple[Any, Any]] = {}
        self._next_id = 0

    def publish(self, snapshot: StateSnapshot) -> None:
        """Store as latest and deliver to every subscriber. Runs on the tick
        thread; must never raise into the tick."""
        with self._lock:
            self._latest = snapshot
            subs = list(self._subs.values())
        for q, loop in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, snapshot)
            except RuntimeError:
                # The subscriber's loop has stopped; it will be cleaned up when
                # its /events handler unwinds and calls unsubscribe().
                pass

    def latest(self) -> StateSnapshot | None:
        with self._lock:
            return self._latest

    def subscribe(self, async_queue: Any, loop: Any) -> int:
        """Register an asyncio.Queue + its loop. Returns a token for unsubscribe.
        The current snapshot is delivered immediately so a new client renders at
        once rather than waiting up to a second for the next tick."""
        with self._lock:
            token = self._next_id
            self._next_id += 1
            self._subs[token] = (async_queue, loop)
            latest = self._latest
        if latest is not None:
            try:
                loop.call_soon_threadsafe(_offer, async_queue, latest)
            except RuntimeError:
                pass
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._subs.pop(token, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def _offer(async_queue: Any, snapshot: StateSnapshot) -> None:
    """Best-effort put onto a bounded async queue: if the client is not draining,
    drop the oldest rather than grow without bound — state is a latest-wins
    stream, so a dropped intermediate snapshot never matters."""
    try:
        async_queue.put_nowait(snapshot)
    except Exception:
        try:
            async_queue.get_nowait()
            async_queue.put_nowait(snapshot)
        except Exception:
            pass
