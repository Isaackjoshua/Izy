# Izy

A local-first desktop focus companion: a small always-on-top mascot that tracks
whether you're working on what you said you'd work on, holds reminders you give
it in natural language, and shows an end-of-day retrospective of where your
attention actually went.

Personal tool, one machine, one person. Nothing leaves the machine.
`SPEC.md` is the source of truth for scope.

**Status: Phase 1 (skeleton + logging).** No LLM calls anywhere in the codebase
yet, and no classification — Phase 1 records what happened and asks you once an
hour whether it was on task, so the `labels` table has real examples in it
before the classifier is written.

## Install

```bash
./packaging/install.sh
```

Then **log out and back in, once**. That step is not optional on GNOME/Wayland
— see below.

```bash
izy doctor     # confirms window titles are actually readable
izy status     # what Izy is tracking right now
izy day -v     # the day's log
```

## The one prerequisite: a GNOME shell extension

On GNOME/Wayland there is no unprivileged way to read the active window title.
Measured on this machine (Ubuntu 24.04, GNOME 46), every route returns nothing:

| Route | Result |
|---|---|
| `xprop -root _NET_ACTIVE_WINDOW` | `0x0` |
| `org.gnome.Shell.Eval` | locked since GNOME 41 |
| `Shell.Introspect.GetWindows` | `AccessDenied` |
| `Shell.Introspect.GetRunningApplications` | `AccessDenied` |
| `Mutter.IdleMonitor.GetIdletime` | works — this is how AFK is detected |

So Izy ships its own ~40-line shell extension (`gnome-extension/izy@local`)
that publishes the focused window on the session bus at `org.izy.Focus`. It is
read-only: it calls getters on the focus window and owns one bus name. This is
the same mechanism ActivityWatch's own Wayland watcher relies on.

GNOME only scans for new extensions at startup, and a Wayland session cannot
restart the shell in place — hence the one logout. `izy doctor` tells you
exactly where you are if it does not come up.

Full measurements: [`docs/STEP0-ENVIRONMENT.md`](docs/STEP0-ENVIRONMENT.md).

## Why the mascot runs under XWayland

Wayland clients cannot set their own position, and Mutter does not implement
layer-shell, so a native Wayland overlay cannot be anchored to a screen corner.
Qt makes this hard to notice: on Wayland it reports back whatever geometry you
requested whether or not the compositor honoured it. Verified against the X
server instead of Qt's cache, XWayland positions correctly and native Wayland
does not, so `izy.service` sets `QT_QPA_PLATFORM=xcb`. Override with `IZY_QPA`.

## Design commitments

These are load-bearing, not stylistic:

- **The interruption budget is code, not guidance.** Max 3 unsolicited
  interruptions per hour, drift must persist 4+ minutes, 15-minute cooldown
  after a dismissal, never during a 20-minute deep-work streak. It reads its
  history from SQLite, so a restart does not hand Izy a fresh allowance.
  Between 3 alerts and 0, it prefers 0. See `izy/budget.py`.
- **No idle animation.** Motion in peripheral vision is exactly what steals
  attention. The only motion in the mascot is a 400ms state cross-fade.
- **Click-through by default**, interactive only under the cursor, and it never
  takes keyboard focus.
- **No generic motivational text**, no guilt language, no exclamation marks, no
  emoji. Tested in `tests/test_ui_smoke.py`.
- **`labels` is the training set.** Every answer you give is a hand-labelled
  example. Nothing deletes from that table.
- **Time is never invented.** If the watcher goes dark, the open span is closed
  at its last confirmed sighting rather than credited with the downtime —
  otherwise every "hours focused" number slowly becomes fiction.

## Layout

```
izy/
  watchers/    Watcher protocol + ActivityWatch and native adapters
  tracker.py   snapshots -> coalesced activity spans (pure, no Qt)
  sessions.py  focus sessions, breaks, restart recovery
  budget.py    interruption budget enforcement
  selflabel.py the hourly "were you on task?" policy
  worker.py    the tracking thread
  ui/          mascot overlay + popups
  cli.py       izy day / status / doctor / start / stop
gnome-extension/izy@local/   the focus reporter
packaging/                   systemd unit + installer
```

Threading: one process. Qt owns the main thread; one worker thread does watcher
polls and SQLite writes and talks to the UI only through Qt signals, so a hung
D-Bus call cannot stutter the mascot. The SQLite connection is created on the
worker thread and never touched from the UI thread.

## Tests

```bash
.venv/bin/python -m pytest
```

Runs fully offline with no `ANTHROPIC_API_KEY` set, and touches no network,
no D-Bus and not your real database. The UI tests skip themselves when there
is no display.

## Config

`~/.config/izy/config.toml`, written with comments on first run, explaining
every knob — especially the interruption budget numbers, which are the ones
worth tuning. There is deliberately no settings GUI.
