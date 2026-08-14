"""Config loading. A commented TOML file is the whole settings surface — there is
deliberately no settings GUI (SPEC.md, Non-goals).

Every knob has a default here, so a missing or partial config file is fine. On
first run we write DEFAULT_CONFIG_TOML out verbatim so the comments land on disk
where they can actually be read while tuning.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_CONFIG_TOML = '''\
# Izy config. Restart the service after editing:  systemctl --user restart izy
# Anything you delete falls back to the built-in default.

[watcher]
# Which activity source to use.
#   "auto"           pick the best available at startup (recommended)
#   "activitywatch"  read the local aw-server REST API
#   "native"         poll the compositor directly
source = "auto"

# How often to sample the focused window, in seconds. 1s is cheap (a D-Bus
# round trip is ~1ms) and gives clean boundaries. Raise it if you ever see the
# poll showing up in power usage.
poll_interval_s = 1.0

# Seconds of no keyboard/mouse input before you count as away. Time spent AFK
# is recorded but never counts as off-task — walking away is not drift.
afk_timeout_s = 180

# An open activity span's duration is flushed to SQLite this often, so a crash
# or a reboot loses at most this many seconds of the span in progress.
flush_interval_s = 15

# Base URL for aw-server, only used when source is "activitywatch"/"auto".
activitywatch_url = "http://localhost:5600"


[session]
# Default length of a focus session in minutes, adjustable per session.
default_minutes = 25

# Default break length in minutes. Classification is off entirely during
# breaks and the mascot stays silent except for due reminders.
break_minutes = 5

# If a session's planned time elapses and you never said how it went, mark it
# ended after this many extra minutes rather than leaving it open forever.
auto_close_after_minutes = 30


[self_label]
# Phase 1 only. Once an hour, ask "were you on task?" about a sampled window,
# to build up the labels table before the classifier exists. Every answer is a
# training example. Set enabled = false to stop being asked.
enabled = true
every_minutes = 60

# Never ask outside these hours (24h clock, local time).
active_from = "09:00"
active_until = "22:00"


# Interruption budget. These are the numbers you will actually tune, and they
# are enforced in code, not treated as guidelines. The failure mode for this
# whole project is that Izy becomes annoying and gets closed permanently, so
# when in doubt these should go DOWN, not up. Between 3 alerts and 0, prefer 0.
[interruptions]
# Hard ceiling on unsolicited interruptions per hour. Nothing can exceed this.
max_per_hour = 3

# Off-task must persist this long before a drift alert is even considered.
# Brief context switches are normal work, not failure.
drift_min_minutes = 4

# After you dismiss an alert, stay quiet for this long.
cooldown_after_dismiss_minutes = 15

# Never interrupt a deep-work streak of at least this many continuous on-task
# minutes. Interrupting focus to protect focus is a bug.
deep_work_protect_minutes = 20


[mascot]
# Which screen corner to anchor to, remembered across restarts.
# One of: top-left, top-right, bottom-left, bottom-right
corner = "bottom-right"

# Gap from the screen edge, in pixels.
margin_px = 24

# Opacity drops to this when the cursor comes within proximity_px, so the
# mascot never blocks anything you are reaching for.
dim_opacity = 0.35
proximity_px = 200

# Normal resting opacity.
opacity = 0.9
'''


@dataclass(frozen=True)
class WatcherConfig:
    source: str = "auto"
    poll_interval_s: float = 1.0
    afk_timeout_s: int = 180
    flush_interval_s: int = 15
    activitywatch_url: str = "http://localhost:5600"


@dataclass(frozen=True)
class SessionConfig:
    default_minutes: int = 25
    break_minutes: int = 5
    auto_close_after_minutes: int = 30


@dataclass(frozen=True)
class SelfLabelConfig:
    enabled: bool = True
    every_minutes: int = 60
    active_from: str = "09:00"
    active_until: str = "22:00"


@dataclass(frozen=True)
class InterruptionConfig:
    max_per_hour: int = 3
    drift_min_minutes: int = 4
    cooldown_after_dismiss_minutes: int = 15
    deep_work_protect_minutes: int = 20


@dataclass(frozen=True)
class MascotConfig:
    corner: str = "bottom-right"
    margin_px: int = 24
    dim_opacity: float = 0.35
    proximity_px: int = 200
    opacity: float = 0.9


@dataclass(frozen=True)
class Config:
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    self_label: SelfLabelConfig = field(default_factory=SelfLabelConfig)
    interruptions: InterruptionConfig = field(default_factory=InterruptionConfig)
    mascot: MascotConfig = field(default_factory=MascotConfig)


_SECTIONS = {
    "watcher": WatcherConfig,
    "session": SessionConfig,
    "self_label": SelfLabelConfig,
    "interruptions": InterruptionConfig,
    "mascot": MascotConfig,
}


def _build(cls, raw: dict[str, Any]):
    """Construct a section, ignoring unknown keys rather than crashing on a typo."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in raw.items() if k in known})


def load(path: Path | None = None, *, write_default: bool = True) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        if write_default:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_CONFIG_TOML)
        return Config()
    raw = tomllib.loads(path.read_text())
    return Config(**{name: _build(cls, raw.get(name, {})) for name, cls in _SECTIONS.items()})
