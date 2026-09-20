# Izy

A local-first desktop focus companion: a small always-on-top mascot that tracks
whether you're working on what you said you'd work on, holds reminders you give
it in natural language, and shows an end-of-day retrospective of where your
attention actually went.

Personal tool, one machine, one  Nothing leaves the machine.
`SPEC.md` is the source of truth for scope.

**Status: all five phases complete.** Izy logs where your attention goes, holds
reminders, judges activity against what you said you were working on, says
something rarely when you drift, and shows you the day afterwards.

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

## Tasks and the Eisenhower matrix

Tasks live in their own table; the quadrant is **derived** from two flags, never
stored — Q1 urgent&important, Q2 important not urgent, Q3 urgent not important,
Q4 neither — so a card dragged between quadrants just sets the flags and there is
never a stored quadrant that disagrees.

```bash
izy task add "write the report" --important --hint-app libreoffice-writer
izy task list
izy start "write the report" --task 3     # a session that knows its task
```

The integration that earns its keep is `hints`: the apps and domains a task
uses. A session started from a task loads them as **free tier-1/2 classification
rules for that session**, so the task's own windows resolve on-task without ever
reaching the paid tier — Izy gets cheaper the more you work a task. When a paid
or asked verdict finds an app on-task for a task-backed session, it is recorded
as a one-tap *suggestion* to add to the hints, never added automatically.

The matrix is also self-observing: once a week the arbiter surfaces one
Important-not-urgent task with no session in seven days — the gap between what
you *labelled* important and what you actually spent time on is the point of the
whole thing. The API exposes tasks (`/tasks`, drag via `/tasks/{id}/quadrant`,
reorder, hint-accept); sessions carry a soft `task_id` link (not an enforced
foreign key, so the daemon and CLI can each write without cross-process
contention).

## The interrupt arbiter

Since the v2 control plane, every would-be interruption — a drift alert, a due
reminder, the hourly self-label, the end-of-session outcome — is a *request*,
and one place (`izy/interrupts/arbiter.py`) decides whether it reaches the
screen. This is the fix for the class of bug that made Izy feel useless: five
features each deciding on their own to talk to you. The tick submits requests;
`arbiter.dispatch()` shows at most one and logs a verdict for every other to
`interrupt_log`, so "why did it nag me at 14:02" is answerable.

Priority high-to-low: urgent reminder, session outcome, due reminder, drift,
self-label, pomodoro, message. Gates, in order: one unacknowledged interrupt
blocks all others; a 90 s global cooldown; quiet hours drop anything below the
outcome prompt; a deep-work streak defers the same; fullscreen defers all but
urgent; **away defers, never drops** (you weren't there to see it); and per-kind
hourly caps. Deferred requests wait in a hold queue and surface the moment their
gate lifts — which is "held to the next natural boundary", generalised.

Configure it in `[interrupts]`. When in doubt those numbers go down.

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

## The mascot

Three static postures in `izy/ui/art.py`, drawn as SVG so they stay crisp at any
scale and ship as code rather than a folder of PNGs:

| State | When | How it reads |
|---|---|---|
| asleep | no session | squat, gray, eyes closed — present but not watching |
| neutral | session running, on task | upright, blue, looking ahead |
| soft-alert | drifting | leaning away, orange, glancing to the side |

Posture carries the state, not just colour, so it stays legible to someone who
cannot distinguish the hues — and the drifting posture looks *away* rather than
disappointed, because off-task is not a moral failure.

**There is no idle animation of any kind.** The only motion the mascot ever
makes is a 400ms cross-fade when its state genuinely changes. The state is
derived every tick from whether you are drifting, so returning to the task
clears it — an earlier version set it when an alert fired and had no way back.

## Screen capture (off by default)

Phase 5's optional tier, and it ships disabled.

When enabled, Izy may photograph the focused window to judge an ambiguous one,
and that image goes to the LLM. The blocklist is checked **before** any capture
happens, so a blocked window's pixels are never read at all — not read and
discarded, *never read*. It fails closed: a window it cannot identify is
refused. The shipped list covers password managers, banking, private browsing
and messaging, and it never photographs Izy's own prompts.

**On GNOME/Wayland there is no silent capture route, and Izy does not pretend
otherwise.** Measured on this machine: `org.gnome.Shell.Screenshot` returns
`AccessDenied`, no screenshot CLI tool is installed, and the only remaining
route is the XDG desktop portal, which prompts every time. That makes this tier
impractical for continuous background use here — which, for a feature like
this, is a reasonable place to land. `izy doctor` reports the status.

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
  ui/          mascot overlay, art, popups
  pipeline.py  the whole tick policy, with no Qt in it
  capture.py   the screen-capture gate (off by default)
  commands/    one module per group of CLI commands
  cli.py       argparse wiring, and nothing else
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
every knob — especially the interruption budget numbers and the classify rule
lists, which are the ones worth tuning. There is deliberately no settings GUI.

The defaults are only written when the file does not exist, so a config from an
earlier version is missing anything added since. `izy config` says whether
yours is current, and `izy config --upgrade` merges in the missing sections and
keys with their comments — it never rewrites a value you have set, keeps your
own comments, and leaves a `.bak` alongside. `izy doctor` mentions it when an
upgrade is available. Nothing does this automatically; silently rewriting a
file you own is not this tool's business.
