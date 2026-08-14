"""ActivityWatchWatcher — reads the local aw-server REST API.

Preferred source when present: ActivityWatch already solves bucket management,
AFK heuristics and (with its browser extension) tab URLs, which is what Tier 2
of the classification ladder will want in Phase 3.

Not installed on this machine as of Step 0, so this adapter is written to the
documented API and exercised by tests against a fake HTTP layer rather than
against a live server. It is selected only if `available()` succeeds, so a
wrong guess about the server degrades to NativeWatcher instead of breaking.
"""
from __future__ import annotations

from typing import Any

from ..models import Snapshot, from_iso, utcnow


class ActivityWatchWatcher:
    name = "activitywatch"

    def __init__(self, base_url: str = "http://localhost:5600", timeout_s: float = 2.0,
                 session=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._session = session
        self._window_bucket: str | None = None
        self._afk_bucket: str | None = None
        self._web_buckets: list[str] = []

    # --- availability ------------------------------------------------------

    def available(self) -> bool:
        info = self._get("/api/0/info")
        if not isinstance(info, dict):
            return False
        buckets = self._get("/api/0/buckets/")
        if not isinstance(buckets, dict):
            return False
        self._classify_buckets(buckets)
        return self._window_bucket is not None

    def _classify_buckets(self, buckets: dict[str, Any]) -> None:
        for bid, meta in buckets.items():
            btype = (meta or {}).get("type", "")
            if btype == "currentwindow" or bid.startswith("aw-watcher-window"):
                self._window_bucket = bid
            elif btype == "afkstatus" or bid.startswith("aw-watcher-afk"):
                self._afk_bucket = bid
            elif btype == "web.tab.current" or bid.startswith("aw-watcher-web"):
                self._web_buckets.append(bid)

    def describe(self) -> str:
        return (f"activitywatch(window={self._window_bucket}, afk={self._afk_bucket}, "
                f"web={len(self._web_buckets)})")

    # --- polling -----------------------------------------------------------

    def poll(self) -> Snapshot | None:
        if not self._window_bucket:
            return None
        win = self._latest_event(self._window_bucket)
        if win is None:
            return None
        data = win.get("data") or {}
        app = data.get("app")
        title = data.get("title")

        afk = False
        if self._afk_bucket:
            ev = self._latest_event(self._afk_bucket)
            if ev:
                afk = (ev.get("data") or {}).get("status") == "afk"

        url = None
        for bucket in self._web_buckets:
            ev = self._latest_event(bucket)
            if ev and (ev.get("data") or {}).get("url"):
                # Only trust the tab URL when the browser is actually focused,
                # otherwise a background tab would masquerade as current activity.
                if _is_browser(app):
                    url = ev["data"]["url"]
                break

        ts = utcnow()
        if win.get("timestamp"):
            try:
                ts = from_iso(str(win["timestamp"]).replace("Z", "+00:00"))
            except ValueError:
                pass

        if not app and not title and not afk:
            return None
        return Snapshot(ts=ts, app=app, title=title, url=url, afk=afk, source=self.name)

    def close(self) -> None:
        if self._session is not None and hasattr(self._session, "close"):
            try:
                self._session.close()
            except Exception:
                pass

    # --- http --------------------------------------------------------------

    def _latest_event(self, bucket: str) -> dict | None:
        events = self._get(f"/api/0/buckets/{bucket}/events?limit=1")
        if isinstance(events, list) and events:
            return events[0]
        return None

    def _get(self, path: str) -> Any:
        try:
            if self._session is None:
                import requests
                self._session = requests.Session()
            r = self._session.get(self.base_url + path, timeout=self.timeout_s)
            if r.status_code != 200:
                return None
            return r.json()
        except Exception:
            return None


_BROWSERS = ("firefox", "chrome", "chromium", "brave", "vivaldi", "edge", "librewolf", "zen")


def _is_browser(app: str | None) -> bool:
    return bool(app) and any(b in app.lower() for b in _BROWSERS)
