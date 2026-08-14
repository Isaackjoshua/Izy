"""Watcher adapters, exercised with the OS and the network faked out.

No D-Bus, no aw-server, no network. That is the point: the suite has to pass on
a machine where none of Step 0's routes work.
"""
from __future__ import annotations

import json

import pytest

from izy.config import WatcherConfig
from izy.models import Snapshot
from izy.watchers import ActivityWatchWatcher, NativeWatcher, pick_watcher
from izy.watchers.base import UnavailableWatcher


# --- NativeWatcher ---------------------------------------------------------

def test_native_reads_focus_from_the_extension(monkeypatch):
    payload = json.dumps({"focused": True, "title": "main.py — Izy",
                          "wm_class": "Code", "wayland": True})
    monkeypatch.setattr("izy.dbus.service_available", lambda dest: True)
    monkeypatch.setattr("izy.dbus.call_string", lambda *a: payload)
    monkeypatch.setattr("izy.dbus.call_uint64", lambda *a: 1000)

    w = NativeWatcher(afk_timeout_s=180)
    assert w.available() is True
    s = w.poll()
    assert s.app == "Code" and s.title == "main.py — Izy" and s.afk is False


def test_native_marks_afk_past_the_timeout(monkeypatch):
    monkeypatch.setattr("izy.dbus.service_available", lambda dest: True)
    monkeypatch.setattr("izy.dbus.call_string",
                        lambda *a: json.dumps({"focused": True, "title": "t", "wm_class": "c"}))
    monkeypatch.setattr("izy.dbus.call_uint64", lambda *a: 200_000)  # 200s idle

    w = NativeWatcher(afk_timeout_s=180)
    w.available()
    assert w.poll().afk is True


def test_native_reports_nothing_when_no_source_works(monkeypatch):
    """The Step 0 state of this machine: no extension, no X11 focus, no idle."""
    monkeypatch.setattr("izy.dbus.service_available", lambda dest: False)
    monkeypatch.setattr("izy.dbus.call_uint64", lambda *a: None)
    monkeypatch.setattr("izy.watchers.native.shutil.which", lambda n: None)

    w = NativeWatcher()
    assert w.available() is False
    assert w.poll() is None


def test_native_survives_malformed_extension_output(monkeypatch):
    monkeypatch.setattr("izy.dbus.service_available", lambda dest: True)
    monkeypatch.setattr("izy.dbus.call_string", lambda *a: "not json{{")
    monkeypatch.setattr("izy.dbus.call_uint64", lambda *a: 0)

    w = NativeWatcher()
    w.available()
    assert w.poll() is None  # no crash, no bogus row


def test_native_falls_back_to_x11_when_the_extension_is_gone(monkeypatch):
    calls = {"n": 0}

    def service_available(dest):
        calls["n"] += 1
        return calls["n"] == 1   # present at startup, gone afterwards

    monkeypatch.setattr("izy.dbus.service_available", service_available)
    monkeypatch.setattr("izy.dbus.call_string", lambda *a: None)
    monkeypatch.setattr("izy.dbus.call_uint64", lambda *a: 0)
    monkeypatch.setattr("izy.watchers.native.shutil.which", lambda n: "/usr/bin/xprop")
    monkeypatch.setattr(NativeWatcher, "_x11_focus",
                        lambda self: ("firefox", "a page — Mozilla Firefox"))

    w = NativeWatcher()
    assert w.available() is True
    w.poll()                       # notices the extension vanished, re-resolves
    s = w.poll()
    assert s is not None and s.app == "firefox"


# --- ActivityWatchWatcher --------------------------------------------------

class FakeHTTP:
    """Stands in for requests.Session."""

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def get(self, url, timeout=None):
        self.seen.append(url)
        path = url.split("5600", 1)[-1]
        body = self.routes.get(path)

        class R:
            status_code = 200 if body is not None else 404
            def json(_self):
                return body
        return R()

    def close(self):
        pass


BUCKETS = {
    "aw-watcher-window_host": {"type": "currentwindow"},
    "aw-watcher-afk_host": {"type": "afkstatus"},
    "aw-watcher-web-firefox": {"type": "web.tab.current"},
}


def _aw(routes):
    return ActivityWatchWatcher("http://localhost:5600", session=FakeHTTP(routes))


def test_activitywatch_available_requires_a_window_bucket():
    w = _aw({"/api/0/info": {"version": "0.12"}, "/api/0/buckets/": {}})
    assert w.available() is False


def test_activitywatch_reads_window_afk_and_url():
    routes = {
        "/api/0/info": {"version": "0.12"},
        "/api/0/buckets/": BUCKETS,
        "/api/0/buckets/aw-watcher-window_host/events?limit=1": [
            {"timestamp": "2026-08-15T10:00:00+00:00",
             "data": {"app": "firefox", "title": "docs — Firefox"}}],
        "/api/0/buckets/aw-watcher-afk_host/events?limit=1": [
            {"data": {"status": "not-afk"}}],
        "/api/0/buckets/aw-watcher-web-firefox/events?limit=1": [
            {"data": {"url": "https://docs.python.org/3/"}}],
    }
    w = _aw(routes)
    assert w.available() is True
    s = w.poll()
    assert s.app == "firefox" and s.afk is False
    assert s.url == "https://docs.python.org/3/"


def test_activitywatch_ignores_tab_url_when_the_browser_is_not_focused():
    """A background tab is not what you are looking at, and letting it through
    would poison Tier 2 of the classifier with URLs you never saw."""
    routes = {
        "/api/0/info": {"version": "0.12"},
        "/api/0/buckets/": BUCKETS,
        "/api/0/buckets/aw-watcher-window_host/events?limit=1": [
            {"data": {"app": "Code", "title": "main.py"}}],
        "/api/0/buckets/aw-watcher-afk_host/events?limit=1": [
            {"data": {"status": "not-afk"}}],
        "/api/0/buckets/aw-watcher-web-firefox/events?limit=1": [
            {"data": {"url": "https://youtube.com/watch"}}],
    }
    s = _aw(routes)
    s.available()
    assert s.poll().url is None


def test_activitywatch_unreachable_is_not_an_exception():
    w = ActivityWatchWatcher("http://localhost:5600", session=FakeHTTP({}))
    assert w.available() is False
    assert w.poll() is None


# --- selection -------------------------------------------------------------

def test_pick_watcher_prefers_activitywatch(monkeypatch):
    monkeypatch.setattr(ActivityWatchWatcher, "available", lambda self: True)
    assert isinstance(pick_watcher(WatcherConfig(source="auto")), ActivityWatchWatcher)


def test_pick_watcher_falls_back_to_native(monkeypatch):
    monkeypatch.setattr(ActivityWatchWatcher, "available", lambda self: False)
    monkeypatch.setattr(NativeWatcher, "available", lambda self: True)
    assert isinstance(pick_watcher(WatcherConfig(source="auto")), NativeWatcher)


def test_pick_watcher_never_raises_when_nothing_works(monkeypatch):
    """A machine with no usable source must still start — the mascot and the
    session log stay useful even with no titles."""
    monkeypatch.setattr(ActivityWatchWatcher, "available", lambda self: False)
    monkeypatch.setattr(NativeWatcher, "available", lambda self: False)
    w = pick_watcher(WatcherConfig(source="auto"))
    assert isinstance(w, UnavailableWatcher)
    assert w.poll() is None


def test_snapshot_key_identifies_a_span():
    from izy.models import utcnow
    a = Snapshot(utcnow(), "code", "main.py")
    b = Snapshot(utcnow(), "code", "main.py")
    c = Snapshot(utcnow(), "code", "other.py")
    assert a.key == b.key and a.key != c.key
