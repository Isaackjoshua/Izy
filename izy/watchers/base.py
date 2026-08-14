"""The Watcher protocol both activity sources implement.

Pull-based by decision: the tracker owns all timing, so each adapter only has
to answer "what is on screen right now?". That keeps coalescing and flush logic
in exactly one place and makes a fake watcher for tests a three-line class.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Snapshot


@runtime_checkable
class Watcher(Protocol):
    #: short identifier recorded on each snapshot, e.g. "native.gnome"
    name: str

    def available(self) -> bool:
        """Can this watcher actually produce data on this machine right now?

        Checked once at startup to pick an adapter. Must not raise, and must be
        cheap enough to call speculatively.
        """

    def poll(self) -> Snapshot | None:
        """Current focused window, or None if nothing could be read this tick.

        Returning None is normal and not an error — no window focused, a
        transient D-Bus hiccup. The tracker treats it as "no change".
        Implementations must not raise; swallow and return None instead.
        """

    def close(self) -> None:
        """Release any connections. Safe to call more than once."""


class UnavailableWatcher:
    """Stands in when no source works, so the app still starts and the mascot
    still runs. Logs nothing but AFK-unknown gaps, which is honest."""

    name = "unavailable"

    def __init__(self, reason: str = "no activity source available") -> None:
        self.reason = reason

    def available(self) -> bool:
        return True

    def poll(self) -> Snapshot | None:
        return None

    def close(self) -> None:
        pass
