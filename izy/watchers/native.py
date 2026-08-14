"""NativeWatcher — reads the compositor directly, no ActivityWatch required.

Step 0 established what is and is not possible on GNOME 46 / Wayland
(see docs/STEP0-ENVIRONMENT.md):

  * Window titles are unavailable through EWMH, Shell.Eval and Shell.Introspect
    alike, so the only route is a shell extension we install ourselves. That is
    `gnome-extension/izy@local`, exposing org.izy.Focus on the session bus.
  * Idle time IS readable, unprivileged, via org.gnome.Mutter.IdleMonitor.

So this class layers three independent sources, each degrading gracefully:

  focus source  extension D-Bus  ->  X11/EWMH (XWayland windows only)  ->  none
  afk source    Mutter IdleMonitor                                     ->  never AFK

Losing the focus source costs titles but still logs AFK; losing the idle source
costs AFK but still logs titles. Neither takes the tracker down.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

from .. import dbus
from ..models import Snapshot, utcnow

IZY_DEST = "org.izy.Focus"
IZY_PATH = "/org/izy/Focus"
IZY_IFACE = "org.izy.Focus"

IDLE_DEST = "org.gnome.Mutter.IdleMonitor"
IDLE_PATH = "/org/gnome/Mutter/IdleMonitor/Core"
IDLE_IFACE = "org.gnome.Mutter.IdleMonitor"


class NativeWatcher:
    name = "native"

    def __init__(self, afk_timeout_s: int = 180) -> None:
        self.afk_timeout_s = afk_timeout_s
        self._focus_source: str | None = None

    # --- availability ------------------------------------------------------

    def available(self) -> bool:
        """True if we can read either focus or idle. Resolves the focus source
        once here so poll() does no probing."""
        self._focus_source = self._detect_focus_source()
        return self._focus_source is not None or self._idle_ms() is not None

    def _detect_focus_source(self) -> str | None:
        if dbus.service_available(IZY_DEST):
            return "extension"
        if shutil.which("xprop") and self._x11_focus() is not None:
            return "x11"
        return None

    def describe(self) -> str:
        src = self._focus_source or "none"
        idle = "idle-monitor" if self._idle_ms() is not None else "no-idle"
        return f"native(focus={src}, afk={idle})"

    # --- polling -----------------------------------------------------------

    def poll(self) -> Snapshot | None:
        idle_ms = self._idle_ms()
        afk = idle_ms is not None and idle_ms / 1000.0 >= self.afk_timeout_s

        app = title = None
        if self._focus_source == "extension":
            got = self._extension_focus()
            if got is None:
                # Extension went away (shell restart, disabled). Re-resolve once
                # so a live X11 fallback can take over instead of going blind.
                self._focus_source = self._detect_focus_source()
            else:
                app, title = got
        elif self._focus_source == "x11":
            got = self._x11_focus()
            if got is not None:
                app, title = got

        if app is None and title is None and not afk:
            return None
        return Snapshot(ts=utcnow(), app=app, title=title, url=None,
                        afk=afk, source=f"{self.name}.{self._focus_source or 'none'}")

    def close(self) -> None:
        pass

    # --- sources -----------------------------------------------------------

    def _idle_ms(self) -> int | None:
        return dbus.call_uint64(IDLE_DEST, IDLE_PATH, IDLE_IFACE, "GetIdletime")

    def _extension_focus(self) -> tuple[str | None, str | None] | None:
        raw = dbus.call_string(IZY_DEST, IZY_PATH, IZY_IFACE, "GetFocused")
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not data.get("focused"):
            # Nothing focused is a real answer, not a failure: no title to log.
            return (None, None)
        return (data.get("wm_class") or data.get("gtk_app_id"), data.get("title"))

    def _x11_focus(self) -> tuple[str | None, str | None] | None:
        """XWayland fallback. Returns None when it cannot see a focused window —
        which on a Wayland session is most of the time, by design."""
        root = _xprop("-root", "_NET_ACTIVE_WINDOW")
        if not root or "#" not in root:
            return None
        wid = root.split("#")[-1].strip().split(",")[0]
        if not wid or wid == "0x0":
            return None
        title = _quoted(_xprop("-id", wid, "_NET_WM_NAME")) or \
            _quoted(_xprop("-id", wid, "WM_NAME"))
        cls_raw = _xprop("-id", wid, "WM_CLASS")
        app = None
        if cls_raw:
            parts = re.findall(r'"([^"]*)"', cls_raw)
            if parts:
                app = parts[-1]
        if not title and not app:
            return None
        return (app, title)


def _xprop(*args: str) -> str | None:
    try:
        r = subprocess.run(["xprop", *args], capture_output=True, text=True, timeout=3)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _quoted(s: str | None) -> str | None:
    if not s or '"' not in s:
        return None
    return s.split('"', 1)[1].rsplit('"', 1)[0] or None
