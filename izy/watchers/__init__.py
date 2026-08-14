"""Watcher selection. Which adapter runs is a runtime decision, per SPEC.md."""
from __future__ import annotations

import logging

from ..config import WatcherConfig
from .activitywatch import ActivityWatchWatcher
from .base import UnavailableWatcher, Watcher
from .native import NativeWatcher

log = logging.getLogger(__name__)

__all__ = ["Watcher", "NativeWatcher", "ActivityWatchWatcher",
           "UnavailableWatcher", "pick_watcher"]


def pick_watcher(cfg: WatcherConfig) -> Watcher:
    """Choose an activity source, preferring ActivityWatch when it is actually
    serving data. Never raises: a machine with no usable source still runs, it
    just logs less."""
    source = (cfg.source or "auto").lower()

    if source in ("auto", "activitywatch"):
        aw = ActivityWatchWatcher(cfg.activitywatch_url)
        if aw.available():
            log.info("activity source: %s", aw.describe())
            return aw
        if source == "activitywatch":
            log.warning("activitywatch requested but unreachable at %s; falling back",
                        cfg.activitywatch_url)

    if source in ("auto", "activitywatch", "native"):
        native = NativeWatcher(cfg.afk_timeout_s)
        if native.available():
            log.info("activity source: %s", native.describe())
            return native

    log.error(
        "no activity source available — window titles will not be logged. "
        "On GNOME/Wayland, install the bundled shell extension: "
        "python -m izy.cli install-extension"
    )
    return UnavailableWatcher()
