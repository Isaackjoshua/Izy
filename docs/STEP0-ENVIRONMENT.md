# Step 0 — Environment probe findings

Run on 2026-08-15. No application code written yet, per `SPEC.md` Step 0.

## 1. System

| | |
|---|---|
| OS | Ubuntu 24.04.4 LTS (Noble Numbat) |
| Kernel | 7.0.0-28-generic, x86_64 |
| Hostname | SilverBlade |
| Desktop | GNOME Shell 46.0 (`ubuntu:GNOME`) |
| Session type | **wayland** (`WAYLAND_DISPLAY=wayland-0`) |
| Compositor | Mutter |
| XWayland | running, `DISPLAY=:0` |
| Python | 3.12.3 (system) |
| `uv` | present at `~/.local/bin/uv` |
| PySide6 | not installed system-wide; installs cleanly (6.11.1) via `uv` |

Spec asks for Python 3.11+; 3.12.3 satisfies it.

## 2. ActivityWatch

**Not installed.** No `aw-server`/`aw-qt`/`aw-watcher-*` binaries, no
`~/.config/activitywatch`, no flatpak or snap, nothing listening on `:5600`
(`curl localhost:5600/api/0/info` → no response).

So `ActivityWatchWatcher` has nothing to read from today. Note this does not
by itself rule ActivityWatch out — but see §3, because upstream ActivityWatch
hits the *same* wall we do on GNOME Wayland and solves it the same way.

## 3. Active window title — THE BLOCKER

This is the risk the spec called out, and it has materialised. Every
unprivileged route was tested; a 10-second once-per-second poll
(`probe/probe_title.py`) returned **0/10 non-empty titles** on every route
except idle time.

| Route | Result |
|---|---|
| `xprop -root _NET_ACTIVE_WINDOW` (XWayland/EWMH) | **`0x0`** — 0/10 polls. `_NET_CLIENT_LIST` is empty. Mutter does not publish focus for native Wayland windows, and nothing X11 was focused. |
| `org.gnome.Shell.Eval` | **`(false, '')`** — locked since GNOME 41 unless unsafe-mode is on. |
| `org.gnome.Shell.Introspect.GetWindows` | **`AccessDenied: GetWindows is not allowed`** — allowlist-gated to specific callers. |
| `org.gnome.Shell.Introspect.GetRunningApplications` | **`AccessDenied`** — same gate. Not even app names, let alone titles. |
| Custom GNOME Shell extension over D-Bus | **Blocked today, but this is the way out.** See below. |
| `org.gnome.Mutter.IdleMonitor.GetIdletime` | ✅ **works**, returns ms since last input. |

So: **AFK detection is solved. Window titles are currently unavailable by any
route.**

### The custom-extension route

I wrote a minimal GNOME 46 extension exposing
`global.display.get_focus_window()` (title, `wm_class`, gtk app id, pid, and
whether the client is native Wayland or XWayland) on the session bus, and
installed it to `~/.local/share/gnome-shell/extensions/`.

GNOME Shell **did not pick it up**: `gnome-extensions enable` reported
*"Extension does not exist"*, and it never appeared in `ListExtensions` even
after touching the directory to nudge the file monitor. The shell only scans
extension directories at **startup**, and on Wayland the shell cannot be
restarted without a full logout/login (`Alt+F2 r` is X11-only).

This is a one-time cost, not a dead end. It is also exactly how upstream
solves it — ActivityWatch's Wayland window watcher and `awatcher` both depend
on a GNOME shell extension for precisely this reason.

I have **removed** the throwaway extension from the live extensions directory
to leave your system as I found it; a copy is preserved in the session
scratchpad, and `probe/` holds the probe scripts so any fix can be re-verified
in seconds.

## 4. Mascot overlay — frameless / translucent / always-on-top / positioned

Tested with real PySide6 6.11.1 windows (`probe/probe_overlay*.py`).

A first pass appeared to succeed under both backends, but that result was a
**false positive**: Qt on Wayland returns whatever geometry you last asked for
whether or not the compositor honoured it. Re-tested against an independent
observer (the X server via `xwininfo`) rather than Qt's own cache:

| Backend | Frameless | Translucent | Always-on-top | Click-through | Self-positioning |
|---|---|---|---|---|---|
| `QT_QPA_PLATFORM=xcb` (XWayland) | ✅ | ✅ | ✅ | ✅ | ✅ **verified by X server: requested (1400,300), got (1400,300)** |
| `QT_QPA_PLATFORM=wayland` (native) | ✅ | ✅ | flag set | flag set | ❌ **unverifiable / not possible** — Wayland clients cannot set their own position, and there is no external observer to check against |

48×56 px at the requested size was granted on both.

**Consequence:** the mascot must run under **XWayland (`QT_QPA_PLATFORM=xcb`)**.
That is a one-line environment setting, keeps the corner-anchoring design in
`SPEC.md` Feature 4 exactly as written, and needs no compositor cooperation.

The usual Wayland-native alternative — `wlr-layer-shell` — is **not** an option
here: Mutter does not implement it. So XWayland is not a shortcut, it is the
only route that preserves the specified design.

## 5. What this means for the build

Nothing in the spec's locked decisions needs to change, and the mascot
placement design survives intact. One prerequisite has to be paid first.

**Recommended path — a purpose-built GNOME extension (one logout).**
Ship a small extension in this repo as a first-class component, install it,
log out and back in once, and `NativeWatcher` reads focus over D-Bus at ~1 Hz.
It is ~40 lines, has no third-party trust surface, gives `wm_class` and pid as
well as the title, and it is the same mechanism ActivityWatch would install
anyway. After the logout, everything in Phase 1 works as specified.

Two alternatives, both worse:

- **Install ActivityWatch + its GNOME extension.** Still needs the same logout,
  adds a large dependency, and buys little that Phase 1 needs — the `Watcher`
  abstraction means we can add `ActivityWatchWatcher` later without rework.
  Worth it *later* for Tier 2 browser-URL classification, which wants the
  ActivityWatch browser extension.
- **Off-the-shelf extension** (*Window Calls*, *Focused Window D-Bus*).
  Installable live from extensions.gnome.org without a logout, but it is
  third-party code running inside your shell, and it is a moving dependency.

### Open questions before Phase 1 — I need your call on these

1. **Which window-title route?** My recommendation: our own extension, one
   logout. Confirm and I'll build it.
2. **Are you willing to log out once?** If not, say so and I'll take the
   off-the-shelf extension route instead.
3. **Meanwhile:** Phase 1 has real work that does not depend on any of this —
   SQLite schema, `Watcher` protocol and both adapters, session start/stop,
   the hourly self-label prompt, the systemd unit, the CLI day-dump. I can
   build all of it behind the abstraction and drop the extension in when you
   decide. Say the word and I'll start there.

Per the working agreement I am stopping here and awaiting your answer, since
the schema shape and the watcher interface are both on the
expensive-to-reverse list.
