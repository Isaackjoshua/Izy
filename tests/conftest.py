"""Shared fixtures.

The whole suite runs offline with no ANTHROPIC_API_KEY set and never touches
the real ~/.local/share/izy database or the session bus.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from izy import config, db
from izy.models import Snapshot


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("IZY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("IZY_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("IZY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def cfg():
    return config.Config()


class FakeClock:
    """Deterministic time. Every module that reads the clock takes it as an
    argument precisely so a day of behaviour can be replayed in milliseconds."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float = 0, minutes: float = 0) -> datetime:
        self.now += timedelta(seconds=seconds, minutes=minutes)
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def snap(clock, app="code", title="main.py", url=None, afk=False) -> Snapshot:
    return Snapshot(ts=clock(), app=app, title=title, url=url, afk=afk, source="fake")
