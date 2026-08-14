"""Minimal session-bus client.

Prefers QtDBus (PySide6 is already a hard dependency, so this costs nothing and
avoids a subprocess per poll) and falls back to shelling out to `gdbus` when Qt
is unavailable — which is what happens in tests and in any headless context.

Only the two shapes Izy actually needs are exposed: a call returning one string
and a call returning one uint64. Both return None on any failure rather than
raising, because a watcher poll must never take the tracker down.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

_TIMEOUT_S = 3


def _qt_call(dest: str, path: str, iface: str, method: str) -> list[Any] | None:
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except Exception:
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        proxy = QDBusInterface(dest, path, iface, bus)
        if not proxy.isValid():
            return None
        proxy.setTimeout(_TIMEOUT_S * 1000)
        reply = proxy.call(method)
        # QDBusMessage.ErrorMessage == 3; compare by name to avoid enum churn.
        if reply.type().name.endswith("ErrorMessage") if hasattr(reply.type(), "name") \
                else int(reply.type()) == 3:
            return None
        return list(reply.arguments())
    except Exception:
        return None


def _gdbus_call(dest: str, path: str, iface: str, method: str) -> str | None:
    if not shutil.which("gdbus"):
        return None
    cmd = ["gdbus", "call", "--session", "--dest", dest,
           "--object-path", path, "--method", f"{iface}.{method}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def call_string(dest: str, path: str, iface: str, method: str) -> str | None:
    args = _qt_call(dest, path, iface, method)
    if args:
        return str(args[0])
    raw = _gdbus_call(dest, path, iface, method)
    if raw is None:
        return None
    # gdbus prints ('...',) — pull out the single quoted string.
    m = re.match(r"^\((['\"])(.*)\1,?\)$", raw, re.DOTALL)
    return m.group(2) if m else None


def call_uint64(dest: str, path: str, iface: str, method: str) -> int | None:
    args = _qt_call(dest, path, iface, method)
    if args:
        try:
            return int(args[0])
        except (TypeError, ValueError):
            return None
    raw = _gdbus_call(dest, path, iface, method)
    if raw is None:
        return None
    m = re.search(r"(\d+)", raw)
    return int(m.group(1)) if m else None


def service_available(dest: str) -> bool:
    """Is a well-known name currently owned on the session bus?"""
    try:
        from PySide6.QtDBus import QDBusConnection
        bus = QDBusConnection.sessionBus()
        if bus.isConnected():
            return bool(bus.interface().isServiceRegistered(dest).value())
    except Exception:
        pass
    if not shutil.which("gdbus"):
        return False
    try:
        r = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus",
             "--object-path", "/org/freedesktop/DBus",
             "--method", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=_TIMEOUT_S)
        return r.returncode == 0 and f"'{dest}'" in r.stdout
    except Exception:
        return False
