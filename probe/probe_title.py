#!/usr/bin/env python3
"""Step 0 throwaway probe: can we read the active window title on this system?

Polls once per second for N seconds via every route available, and reports
which ones return a real title vs. an empty string / 0x0.
NOT application code. Delete after Step 0.
"""
import json
import subprocess
import sys
import time

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 10


def sh(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception as e:
        return f"ERR:{e}"


def route_x11():
    """XWayland EWMH. Only ever sees X11 clients, never native Wayland ones."""
    out = sh("xprop -root _NET_ACTIVE_WINDOW")
    if "0x0" in out or "#" not in out:
        return None, "no active X11 window (0x0)"
    wid = out.split("#")[-1].strip().split(",")[0]
    name = sh(f"xprop -id {wid} _NET_WM_NAME")
    cls = sh(f"xprop -id {wid} WM_CLASS")
    if '"' not in name:
        return None, f"{wid}: no _NET_WM_NAME"
    title = name.split('"', 1)[1].rsplit('"', 1)[0]
    wm = cls.split('"')[-2] if '"' in cls else "?"
    return title, f"x11 wm_class={wm}"


def route_gnome_introspect():
    """GNOME's own D-Bus window introspection."""
    out = sh("gdbus call --session --dest org.gnome.Shell "
             "--object-path /org/gnome/Shell/Introspect "
             "--method org.gnome.Shell.Introspect.GetWindows")
    if "AccessDenied" in out:
        return None, "AccessDenied (allowlist-gated)"
    return out[:120], "ok"


def route_ext():
    """A shell extension we control, exposing focus window over D-Bus."""
    out = sh("gdbus call --session --dest org.izy.Probe --object-path /org/izy/Probe "
             "--method org.izy.Probe.GetFocused")
    if "ServiceUnknown" in out or "Error" in out:
        return None, "extension not loaded"
    try:
        payload = json.loads(out.strip().strip("(),").strip("'"))
    except Exception:
        return out[:100], "unparsed"
    if not payload.get("focused"):
        return None, "no focused window"
    kind = "wayland" if payload.get("wayland") else "xwayland"
    return payload.get("title"), f'{kind} wm_class={payload.get("wm_class")}'


def route_idle():
    out = sh("gdbus call --session --dest org.gnome.Mutter.IdleMonitor "
             "--object-path /org/gnome/Mutter/IdleMonitor/Core "
             "--method org.gnome.Mutter.IdleMonitor.GetIdletime")
    digits = "".join(c for c in out if c.isdigit())
    return (f"{int(digits)/1000:.1f}s idle", "ok") if digits else (None, out[:60])


ROUTES = [
    ("x11/xprop     ", route_x11),
    ("gnome-introspect", route_gnome_introspect),
    ("shell-extension", route_ext),
    ("idle-monitor  ", route_idle),
]

print(f"Polling {DUR}s — switch between a few different apps now.\n")
hits = {n: 0 for n, _ in ROUTES}
seen = {n: set() for n, _ in ROUTES}

for i in range(DUR):
    print(f"--- t={i+1}s ---")
    for name, fn in ROUTES:
        val, note = fn()
        if val:
            hits[name] += 1
            seen[name].add(str(val)[:80])
        print(f"  {name}: {str(val)[:70]!r:<74} [{note}]")
    time.sleep(1)

print("\n================ SUMMARY ================")
for name, _ in ROUTES:
    print(f"{name}: {hits[name]}/{DUR} non-empty, {len(seen[name])} distinct value(s)")
    for s in sorted(seen[name])[:8]:
        print(f"     - {s}")
