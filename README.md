# Izy

A local-first desktop focus companion: a small always-on-top mascot that tracks
whether you're working on what you said you'd work on, holds reminders you give
it in natural language, and shows an end-of-day retrospective of where your
attention actually went.

Personal tool, one machine, one person. Nothing leaves the machine.
`SPEC.md` is the source of truth for scope.

**Status: Phase 4 (retrospective) complete.** Izy logs where your attention
goes, holds reminders, judges activity against what you said you were working
on, says something rarely when you drift, and shows you the day afterwards.
Phase 5 is mascot art and the optional screen-capture tier.

## Install

```bash
./packaging/install.sh
```

Then **log out and back in, once**. That step is not optional on GNOME/Wayland
— see below.

```bash
izy doctor      # confirms window titles are actually readable
izy status      # what Izy is tracking right now
izy day -v      # the day's log, including LLM spend
izy remind me to email the supervisor at 4pm
izy reminders   # what is pending
```

## Reminders

Tell Izy something and it hands it back at the right moment. Type it into the
mascot's box prefixed with "remind me", or use the CLI:

```
remind me to email the supervisor at 4pm
remind me in 20 minutes to check the training run
remind me next time I take a break to refill water
remind me when this session ends to push the branch
remind me next time I open slack to reply to the PR thread
```

Parsing is a ladder, cheapest first: rules for context triggers, `dateparser`
for times, and only then a single LLM call. **It never invents a time that you
did not state** — if nothing is readable it asks, and a budget refusal asks too
rather than degrading to a guess. In practice every example above parses for
free, with no API call at all.

A reminder that comes due mid-session is **held until the next natural
boundary** (session end or break), because a reminder that breaks the focus it
exists to protect is a bug. Mark one urgent to override that. Held reminders are
released anyway once they are `max_defer_minutes` overdue. Firing shows a small
bubble with done / snooze / dismiss — never modal, never focus-stealing, silent.

Reminders are things you asked for, so they do not consume the interruption
budget, which is reserved for things Izy decides to say on its own.

## The retrospective

```bash
izy report              # build today's dashboard and open it
izy report yesterday
izy report --no-serve   # just write the HTML file
```

A local HTML page — self-contained, no CDN, no fonts, no network of any kind,
so it renders with the machine offline. It shows the day's timeline as
on-task / off-task / break / away bands (hover for the app and window), the
session table with planned against actual, which app pulled you out and how
much time went with it, which hours you are weakest in (with sample sizes,
because one bad ten-minute hour is not a pattern), the full tier 3/4
classification audit with each decision's reason, and the day's LLM spend.

It is also written automatically at the end of each day to
`~/.local/share/izy/reports/`, quietly — nothing is opened and nothing is
announced.

**Correcting a wrong call is one click.** `izy report` serves the page from
127.0.0.1 so the "this was wrong" buttons can write straight to `labels`; the
server binds to loopback only and answers exactly two routes. Opened as a plain
`file://` there is no server to talk to, so the button tells you the
`izy relabel` command instead of failing silently.

The colours are the validated categorical palette (blue / orange / aqua) checked
against both light and dark surfaces. Away is neutral gray rather than a fourth
hue — being away is an absence of activity, not a kind of it — which also keeps
the palette to three hues and clear of the colour-vision floors. Aqua sits below
3:1 on the light surface, so identity is never carried by colour alone: every
band is named in the legend and again in the totals table.

## Classification

Activity is judged against **what you said you were working on**, not against
productivity in the abstract — reading docs or searching an error message is
on-task for a programming session. The ladder stops at the first confident
answer, cheapest first:

| Tier | Input | Cost |
|---|---|---|
| 1 | app + window title vs. your allow/deny rules | free |
| 2 | browser tab URL | free |
| 3 | one LLM call, batched and cached | paid |
| 4 | asks you, one tap | free |

Cost discipline is enforced, not hoped for: spans under `min_duration_s` are
never judged, events with no declared intent never reach tier 3, ambiguous
events are buffered and judged several per call, and the cache is keyed on
`(intent, app, normalized_title)` so the same window never costs twice in a
session. Measured on a simulated working day in `tests/test_classifier.py`:
**well under the 50-call budget**, roughly one call per session after caching.

**On exceeding the budget it asks you. It never degrades to a guess.** The same
is true when there is no API key, when the model is unsure (below
`confidence_threshold`), and when a verdict comes back missing.

Add to the `on_task_apps` / `off_task_apps` / `*_urls` lists in the config
whenever tier 3 or tier 4 asks about something you consider obvious — every
rule you add is a call you never pay for again.

Correct a wrong call with `izy relabel <event-id> on|off`; `izy day` prints the
audit of every tier 3 and tier 4 decision with its reason.

## Drift alerts

The only thing Izy says unprompted about your work, and deliberately hard to
trigger. It names what you declared and what you are doing instead, and nothing
else:

```
You said: fix the dataloader. YouTube, 11 min.
```

Off-task must persist 4+ minutes; at most 3 unsolicited interruptions an hour,
ever; 15-minute cooldown after you dismiss one; never during a 20-minute
deep-work streak; **one alert per drift run, not one per tick**; and walking
away is not drift. Set `drift.enabled = false` to classify silently and only
see it in the retrospective.

## LLM calls

Every LLM call in the codebase goes through `izy/llm.py`, which enforces
caching, an hourly and a daily call ceiling, and logs every call with its token
count and cost — visible in `izy day`. `tests/test_llm.py` asserts by source
scan that no other module reaches the API. With no `ANTHROPIC_API_KEY` set,
Izy still runs; it just asks you instead of guessing.

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
  llm.py       the ONLY module that talks to the Anthropic API
  rules.py     tiers 1-2: the free classification rules
  classifier.py  the ladder, batching, caching, tier 4 escalation
  drift.py     when Izy is allowed to say you have drifted
  reminders/   natural-language parsing, storage, firing rules
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

Every worker signal is received by the `UiBridge` QObject in `app.py`. That is
load-bearing, not tidiness: Qt picks a slot's thread from the *receiver's*
affinity, and a plain Python function has none, so connecting to bare functions
ran UI code on the worker thread and segfaulted the daemon.

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
