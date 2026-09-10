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


# LLM. Every call in Izy goes through izy/llm.py, which enforces these.
# Nothing is sent anywhere unless a call actually happens, and Phase 2 only
# calls out when dateparser cannot understand a reminder you typed.
[llm]
# Set false to disable LLM calls entirely. Izy still works — it just asks you
# instead of guessing when it cannot parse something.
enabled = true

# Needs ANTHROPIC_API_KEY in the environment. No key means no calls, and Izy
# degrades to asking rather than failing.
model = "claude-opus-5"

# Thinking depth. Reminder parsing is a small extraction task, so "low" is
# plenty; raise it only if you see parses going wrong.
effort = "low"
max_tokens = 1024

# Hard call ceilings, deliberately low. On exceeding these Izy asks you rather
# than degrading to a guess. Cache hits are free and are not counted.
max_calls_per_hour = 10
max_calls_per_day = 50


[reminders]
# Minutes a "snooze" defers a reminder by.
snooze_minutes = 10

# A reminder that fires during a focus session breaks the focus it is supposed
# to protect. Non-urgent reminders that come due mid-session are therefore held
# until the next natural boundary (session end or break). Set false to have
# them fire immediately regardless — not recommended.
defer_during_focus = true

# How long past its due time a held reminder still fires. Beyond this it is
# stale and fires at the next boundary anyway rather than being dropped.
max_defer_minutes = 60

# The hour "end_of_day" context reminders fire at (24h clock, local).
end_of_day_hour = 18


# Classification. Evaluated as a ladder, stopping at the first confident
# answer, because cost discipline is a hard requirement:
#   tier 1  app + title vs the rules below          free
#   tier 2  browser tab URL                         free
#   tier 3  one LLM call, only when 1-2 are unsure  paid
#   tier 4  ask you, one tap                        free
[classify]
enabled = true

# Spans shorter than this are never classified. Glancing at a window for four
# seconds is not a decision worth paying to judge.
min_duration_s = 20

# Ambiguous events are buffered for up to this long and judged several per
# call, rather than one call each.
batch_window_s = 60

# Below this confidence Izy asks you (tier 4) instead of trusting the answer.
confidence_threshold = 0.7

# Tier 1 rules. Matching is case-insensitive substring by default; prefix an
# entry with "re:" for a regular expression. Deny wins over allow, so listing
# an app as off-task beats a matching on-task title.
#
# These are absolute judgements ("Spotify is never work"), unlike tier 3, which
# asks whether something is plausibly related to what you *said* you are doing.
# Most of your day should be handled here, for free — add to these lists
# whenever tier 3 or tier 4 asks about something you consider obvious.
on_task_apps = ["code", "kitty", "alacritty", "gnome-terminal", "jetbrains", "pycharm"]
off_task_apps = ["spotify", "steam", "discord", "vlc"]
on_task_titles = []
off_task_titles = []

# Tier 2. Only consulted when the focused window is a browser.
on_task_urls = ["localhost", "github.com", "docs.python.org", "stackoverflow.com"]
off_task_urls = ["youtube.com", "twitter.com", "x.com", "reddit.com", "instagram.com",
                 "tiktok.com", "netflix.com", "facebook.com"]


# Drift alerts. These ride on the interruption budget above, so they can never
# exceed max_per_hour however much you drift.
[drift]
enabled = true

# Alerts name what you said you were doing and what you are doing instead, and
# nothing more: "You said: fix the dataloader. YouTube, 11 min."
# Set false to classify silently and only see it in the retrospective.


# Screen capture. Off by default, and it should probably stay that way.
#
# When enabled, Izy may photograph the focused window to help judge an
# otherwise ambiguous one, and that image is sent to the LLM. The blocklist
# below is checked BEFORE any capture happens, so a blocked window's pixels are
# never read at all — not read and discarded, never read.
#
# On GNOME/Wayland there is no silent capture route: the shell's screenshot
# D-Bus method is AccessDenied and the only alternative is the desktop portal,
# which asks you every time. In practice that makes this tier impractical here,
# which for a feature like this is a reasonable place to land.
[capture]
enabled = false

# Matched case-insensitively as substrings against the app and the window
# title; prefix an entry with "re:" for a regular expression. Leave the list
# empty to use the built-in default, which covers password managers, banking,
# private browsing and messaging. Add to it freely — a missed capture only
# costs one extra question.
blocklist = []


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
class LLMConfig:
    enabled: bool = True
    model: str = "claude-opus-5"
    effort: str = "low"
    max_tokens: int = 1024
    max_calls_per_hour: int = 10
    max_calls_per_day: int = 50


@dataclass(frozen=True)
class RemindersConfig:
    snooze_minutes: int = 10
    defer_during_focus: bool = True
    max_defer_minutes: int = 60
    end_of_day_hour: int = 18


@dataclass(frozen=True)
class ClassifyConfig:
    enabled: bool = True
    min_duration_s: int = 20
    batch_window_s: int = 60
    confidence_threshold: float = 0.7
    # Defaults deliberately mirror DEFAULT_CONFIG_TOML exactly, so a machine
    # with no config file behaves identically to one with the shipped file.
    # tests/test_selflabel_and_config.py asserts that round-trip.
    on_task_apps: tuple = ("code", "kitty", "alacritty", "gnome-terminal",
                           "jetbrains", "pycharm")
    off_task_apps: tuple = ("spotify", "steam", "discord", "vlc")
    on_task_titles: tuple = ()
    off_task_titles: tuple = ()
    on_task_urls: tuple = ("localhost", "github.com", "docs.python.org",
                           "stackoverflow.com")
    off_task_urls: tuple = ("youtube.com", "twitter.com", "x.com", "reddit.com",
                            "instagram.com", "tiktok.com", "netflix.com",
                            "facebook.com")


@dataclass(frozen=True)
class DriftConfig:
    enabled: bool = True


@dataclass(frozen=True)
class CaptureConfig:
    enabled: bool = False
    blocklist: tuple = ()


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
    llm: LLMConfig = field(default_factory=LLMConfig)
    reminders: RemindersConfig = field(default_factory=RemindersConfig)
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)
    drift: DriftConfig = field(default_factory=DriftConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    mascot: MascotConfig = field(default_factory=MascotConfig)


_SECTIONS = {
    "watcher": WatcherConfig,
    "session": SessionConfig,
    "self_label": SelfLabelConfig,
    "interruptions": InterruptionConfig,
    "llm": LLMConfig,
    "reminders": RemindersConfig,
    "classify": ClassifyConfig,
    "drift": DriftConfig,
    "capture": CaptureConfig,
    "mascot": MascotConfig,
}


def _build(cls, raw: dict[str, Any]):
    """Construct a section, ignoring unknown keys rather than crashing on a typo.

    TOML arrays parse as lists; the sections are frozen dataclasses compared for
    equality in tests, so lists are normalised to tuples.
    """
    known = {f.name for f in fields(cls)}
    values = {k: (tuple(v) if isinstance(v, list) else v)
              for k, v in raw.items() if k in known}
    return cls(**values)


def load(path: Path | None = None, *, write_default: bool = True) -> Config:
    path = path or paths.config_path()
    if not path.exists():
        if write_default:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_CONFIG_TOML)
        return Config()
    raw = tomllib.loads(path.read_text())
    return Config(**{name: _build(cls, raw.get(name, {})) for name, cls in _SECTIONS.items()})
