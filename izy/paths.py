"""Where Izy keeps its things. XDG, with env overrides for tests."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "izy"


def _xdg(env_var: str, default: str) -> Path:
    return Path(os.environ.get(env_var) or Path.home() / default)


def data_dir() -> Path:
    """~/.local/share/izy — the SQLite file lives here."""
    if override := os.environ.get("IZY_DATA_DIR"):
        return Path(override)
    return _xdg("XDG_DATA_HOME", ".local/share") / APP_NAME


def config_dir() -> Path:
    """~/.config/izy — the commented TOML config lives here."""
    if override := os.environ.get("IZY_CONFIG_DIR"):
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config") / APP_NAME


def state_dir() -> Path:
    """~/.local/state/izy — mascot corner, last-run markers."""
    if override := os.environ.get("IZY_STATE_DIR"):
        return Path(override)
    return _xdg("XDG_STATE_HOME", ".local/state") / APP_NAME


def db_path() -> Path:
    return data_dir() / "data.db"


def config_path() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), state_dir()):
        d.mkdir(parents=True, exist_ok=True)
