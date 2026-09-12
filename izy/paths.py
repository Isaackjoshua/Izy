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


def runtime_dir() -> Path:
    """$XDG_RUNTIME_DIR/izy — the control-plane socket lives here.

    izy-v2.md §1 requires a Unix domain socket, never a TCP port. XDG_RUNTIME_DIR
    is the right home: it is user-private (mode 0700), tmpfs-backed, and cleared
    on logout, so a stale socket never survives a session. Falls back to the
    state dir on the rare system where it is unset (e.g. a bare cron context)."""
    if override := os.environ.get("IZY_RUNTIME_DIR"):
        return Path(override) / APP_NAME
    base = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(base) / APP_NAME) if base else (state_dir() / "run")


def socket_path() -> Path:
    return runtime_dir() / "izy.sock"


def db_path() -> Path:
    return data_dir() / "data.db"


def config_path() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), state_dir()):
        d.mkdir(parents=True, exist_ok=True)
    # The runtime dir is private; the socket under it is chmod 0600 by the API.
    runtime_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
