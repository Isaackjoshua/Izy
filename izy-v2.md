# Izy v2 — Master Implementation Prompt

---

## 0. Context the agent must absorb first

Izy is a background daemon (`izy.service`) that samples the focused window once per second and, **only during a declared focus session**, judges whether that activity matches the stated intent. Everything routes through one tick loop in `pipeline.py`. There is no other engine.

Current tick, in order:

1. `resync` — pick up sessions started/stopped from the CLI in another process
2. poll watcher → focused window (app + title), or AFK
3. record activity span (identical consecutive windows coalesce into one row)
4. `maybe_overrun` — planned time elapsed → end session, ask outcome
5. `maybe_self_label` — hourly "were you on task?" prompt
6. `check_reminders`
7. `classify` — judge spans that just closed
8. `check_drift`
9. `update_mascot`

Mascot is a pure readout of `mascot_state`, recomputed every tick: **asleep** (no session) / **neutral** (session, on task) / **soft-alert** (session, drifting ≥ 4 min).

**Classification ladder** (only with an active session): tier 1 app/title rules (free) → tier 2 browser URL (free) → tier 3 one LLM call (paid) → tier 4 ask the user one tap. Stops at first confident answer.

**Drift:** ≥ 4 continuous off-task minutes → one alert, capped 3/hour, 15-min cooldown, never during a 20-min deep-work streak.

### Invariants you may not break

- **The tick loop stays the only engine.** Every new feature is a step in the tick or a pure function called by one. No second scheduler, no `threading.Timer`, no `asyncio` loop racing the tick.
- **Off a session, Izy is asleep.** No classification, no drift, no tier-3 calls. New features must not wake the judging half. (The message library is the one thing allowed to speak while asleep — and only through the arbiter in Phase 2.)
- **The daemon must stay headless-capable.** It runs fine with no GUI attached. The dashboard and widget are *clients*. If they're closed, nothing about tracking changes.
- **Tier-3 spend does not go up.** New features may only *reduce* tier-3 calls (see task hints, Phase 3).
- **The CLI keeps working.** `izy start`, `izy stop`, `izy remind`, `izy reminders`, `izy report` must behave identically after every phase.
- **Local only.** SQLite + a Unix domain socket. No TCP port, no cloud, no telemetry.

### Stack (decided — do not re-litigate)

Ubuntu, GNOME, Wayland. The existing UI is PySide6.

| Piece | Choice | Why |
|---|---|---|
| Daemon | unchanged Python | it works |
| IPC | FastAPI on a **Unix domain socket** (`$XDG_RUNTIME_DIR/izy/izy.sock`), uvicorn `--uds`, mode `0600` | no open port; one API serves dashboard, widget and CLI |
| Dashboard | **PySide6 / Qt Widgets** | same language and process family as the mascot; one venv |
| Floating widget | **PySide6 + QtQuick (QML)** | GPU shaders, `MultiEffect` blur, 60 fps ring animation — QWidget painting cannot do convincing glass |
| DB | existing SQLite, versioned migrations | |

**Wayland reality check, state this in the code comments:** GNOME's Mutter does not implement `wlr-layer-shell` and does not blur behind windows. So:

- The floating widget runs under **XWayland** (`QT_QPA_PLATFORM=xcb` for that process only) so `Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool` and client-side positioning actually work.
- "Liquid glass" is **composited by us**, not by the compositor. Recipe in Phase 7.
- A GNOME Shell extension using `Shell.BlurEffect` is the only route to true live backdrop blur. That is an optional Phase 8, not a dependency.

---

## 1. Phase 1 — Control plane (`izyd` API)

Nothing else can be built until the daemon can be talked to. Right now state lives in the daemon's memory and the CLI reaches it through `resync`, which is fine for two processes and hopeless for four.

**Build:**

- `izy/api/` — FastAPI app served by uvicorn on the UDS, started *inside* the daemon process as a background task (so there is exactly one owner of the tick loop and the DB writer).
- A `StateBus`: the tick's final step publishes an immutable snapshot; subscribers get deltas.

**Endpoints:**

```
GET  /state                    # mascot posture, active session, pomodoro, today's counters
WS   /events                   # state deltas (on change) + 1 Hz tick beat + interrupt pushes
GET  /report?date=&range=

GET/POST      /tasks
PATCH/DELETE  /tasks/{id}
POST          /tasks/reorder

POST /sessions            {intent, minutes, task_id?}
POST /sessions/current/stop
POST /sessions/current/outcome   {finished|partly|no}

POST /pomodoro/start      {task_id?, config_id?}
POST /pomodoro/pause | /resume | /skip | /stop
GET/PUT /pomodoro/config

GET/POST      /messages
PATCH/DELETE  /messages/{id}
POST          /messages/{id}/test     # fire it now, bypassing cadence but NOT the arbiter
GET/PUT       /messages/rules

POST /interrupts/{id}/ack | /snooze | /dismiss
GET  /doctor                   # socket, db, migrations, watcher, XWayland, tier-3 budget
```

**Rules:**

- The API **never writes to the DB directly**. It enqueues a command; the tick drains the queue at step 1 (`resync` becomes `drain_commands`). One writer, no locking bugs, no state that disagrees with the mascot.
- `resync` is thereby generalised, not replaced — CLI-originated changes keep working through the same path.
- Responses are typed Pydantic models shared with the clients via a generated JSON schema.

**Phase gate:** `curl --unix-socket … /state` returns live data; `izy start` from a terminal is visible on `/events` within one tick; killing the API task does not stop tracking.

---

## 2. Phase 2 — The interrupt arbiter (do this before any new notification)

The hourly self-label spam was not a bug in `maybe_self_label`. It was a bug in there being **four independent things allowed to interrupt you** with no shared budget. Adding a motivational-message engine on top of that would make it five and turn Izy into the thing it's supposed to protect you from.

**Build `izy/interrupts/arbiter.py`.** Every would-be interruption becomes a *request*, and exactly one place decides.

```python
Request(kind, priority, payload, dedupe_key, expires_at)
Verdict = SHOW | DEFER | DROP
```

Priority ladder:

| Priority | Kind |
|---|---|
| 100 | urgent reminder |
| 90 | session overrun / outcome prompt |
| 70 | due reminder |
| 50 | drift alert |
| 30 | hourly self-label |
| 20 | pomodoro phase transition |
| 10 | message library |

Global gates, applied in this order:

1. **One at a time.** An unacknowledged interrupt blocks all others.
2. **Global cooldown** ≥ 90 s between any two shown interrupts.
3. **Quiet hours** (configurable) — everything below 90 is dropped.
4. **Deep-work protection** — during a ≥ 20-min on-task streak, anything below 90 defers.
5. **Fullscreen / screen-share / presentation detected** → defer everything below 100.
6. **AFK** → defer, never drop (you weren't there to see it).
7. **Per-kind hourly caps** — drift 3/h (existing), self-label 1/h, library messages configurable and default 2/h.
8. **Per-message cooldown** (Phase 5).

Deferred requests go to a hold queue drained at the **next natural boundary**: session end, break start, or AFK-return. That is exactly the behaviour reminders already have ("held to the next break") — generalise it, don't duplicate it.

**Rewire:** `maybe_self_label`, `check_reminders`, `check_drift` stop showing things themselves. They *submit requests*. A new final-ish tick step `arbiter.dispatch()` is the single place anything reaches the screen.

New tick order:

```
1  drain_commands        (was resync)
2  poll watcher
3  record span
4  maybe_overrun         -> submit
5  tick_pomodoro         -> submit          [Phase 4]
6  maybe_self_label      -> submit
7  check_reminders       -> submit
8  check_messages        -> submit          [Phase 5]
9  classify
10 check_drift           -> submit
11 arbiter.dispatch()    <- only place that shows anything
12 update_mascot
13 publish_state
```

Log every decision to `interrupt_log(kind, priority, requested_at, verdict, reason)`. The Reports screen gets an "interruptions" panel from this, and you can finally answer "why did it nag me at 14:02".

**Phase gate:** a test that fires all seven kinds in the same tick shows exactly one, in priority order, and logs six deferrals with reasons.

---

## 3. Phase 3 — Tasks + Eisenhower matrix

**Schema (migration `00X_tasks.sql`):**

```sql
CREATE TABLE task (
  id            INTEGER PRIMARY KEY,
  title         TEXT    NOT NULL,
  notes         TEXT,
  urgent        INTEGER NOT NULL DEFAULT 0,
  important     INTEGER NOT NULL DEFAULT 0,
  status        TEXT    NOT NULL DEFAULT 'todo',   -- todo|doing|done|dropped
  due_at        INTEGER,
  estimate_pomos INTEGER,
  actual_pomos  INTEGER NOT NULL DEFAULT 0,
  hints         TEXT,                              -- JSON {"apps":[],"domains":[],"keywords":[]}
  parent_id     INTEGER REFERENCES task(id) ON DELETE CASCADE,
  sort_key      REAL    NOT NULL,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  completed_at  INTEGER
);
CREATE INDEX task_status_idx   ON task(status, urgent, important);
CREATE INDEX task_due_idx      ON task(due_at) WHERE due_at IS NOT NULL;

ALTER TABLE session ADD COLUMN task_id INTEGER REFERENCES task(id);
```

Quadrant is **derived**, never stored: `Q1 = urgent & important`, `Q2 = important & !urgent`, `Q3 = urgent & !important`, `Q4 = neither`.

**Behaviour:**

- Dragging a card between quadrants sets the two flags. That is the only way flags change automatically.
- A due date inside 24 h produces a **suggestion chip** ("looks urgent — move to Q1?"), never an automatic move. Eisenhower is only useful if the classification is yours.
- Q2 is the point of the whole matrix: if no Q2 task has had a session in 7 days, surface one card on Today — "you haven't touched *Important, not urgent* this week." Once per week, priority 10, through the arbiter.
- Q4 items older than 14 days get a "delete?" prompt in the UI only (never a notification).
- Subtasks via `parent_id`, one level deep. Completing all children prompts to complete the parent.

**The integration that earns its keep — `task.hints`:**

When a session is started from a task, load that task's `hints` as **tier-1 and tier-2 rules for that session only**. `{"apps":["code","alacritty"],"domains":["docs.python.org","github.com"]}` means those windows resolve on-task at tier 1/2 for free, and never reach the paid tier-3 call. Every task you work on more than twice should end up with hints.

Better: when tier 3 or tier 4 resolves a span as on-task for a task-backed session, **offer to add that app/domain to the task's hints** (a one-tap chip on the tier-4 prompt, or a batch suggestion on the Today screen). Izy gets cheaper the more you use it. That's the feature.

**Phase gate:** create a task, start a session from it, confirm the classification ladder short-circuits at tier 1 for a hinted app and the tier-3 counter does not move.

---

## 4. Phase 4 — Pomodoro

**The key design decision: a pomodoro *is* a session.** Not a parallel timer. Starting a pomodoro calls the same `start_session` path with `kind='pomodoro'`, `intent = task.title`, `minutes = work_min`. This means classification, drift, the mascot and the report all work on pomodoros for free, with zero new code. Do not build a second timer that the daemon doesn't know about.

**Schema:**

```sql
CREATE TABLE pomo_config (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  work_min INTEGER NOT NULL DEFAULT 25,
  short_break_min INTEGER NOT NULL DEFAULT 5,
  long_break_min  INTEGER NOT NULL DEFAULT 15,
  long_break_every INTEGER NOT NULL DEFAULT 4,
  auto_start_breaks INTEGER NOT NULL DEFAULT 1,
  auto_start_work   INTEGER NOT NULL DEFAULT 0,
  is_default INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE pomo_cycle (
  id INTEGER PRIMARY KEY,
  task_id INTEGER REFERENCES task(id),
  config_id INTEGER NOT NULL REFERENCES pomo_config(id),
  state TEXT NOT NULL,          -- work|short_break|long_break|paused|done
  phase_started_at INTEGER NOT NULL,
  phase_ends_at    INTEGER NOT NULL,
  paused_at        INTEGER,
  completed_pomos  INTEGER NOT NULL DEFAULT 0,
  started_at INTEGER NOT NULL, ended_at INTEGER
);
ALTER TABLE session ADD COLUMN kind TEXT NOT NULL DEFAULT 'focus';  -- focus|pomodoro|break
ALTER TABLE session ADD COLUMN pomo_cycle_id INTEGER REFERENCES pomo_cycle(id);
```

**`tick_pomodoro` (tick step 5)** — a pure state machine, no timers:

- Elapsed compared against `phase_ends_at` every tick. Time is derived from wall-clock stamps, **never accumulated**, so suspend/resume and clock changes can't drift the timer.
- Work phase ends → `completed_pomos += 1`, `task.actual_pomos += 1`, session ends with outcome prompt suppressed (the pomodoro *is* the answer), submit a priority-20 transition interrupt, start break if `auto_start_breaks`.
- Break phases start a session with `kind='break'` — during which **the mascot goes to a fourth posture, `resting`** (green, eyes half-closed), and **classification and drift are disabled**. A break is not drift. This is the one sanctioned extension to `mascot_state`; implement it inside that function so the mascot stays a pure readout.
- Pause stamps `paused_at` and pushes `phase_ends_at` forward on resume.
- Every 4th (`long_break_every`) work phase → long break.
- Interaction with `maybe_overrun`: a pomodoro-backed session is ended by `tick_pomodoro`, so `maybe_overrun` must skip sessions where `kind='pomodoro'`. Guard this explicitly or you'll get two end-of-session prompts.

**Phase gate:** run a 1-min/1-min config for three cycles. Exactly one interrupt per transition, correct long break on the 4th, `actual_pomos` matches, no drift alerts during breaks, `izy report` shows the cycles.

---

## 5. Phase 5 — Editable message library

A store of short lines the daemon surfaces on a cadence — encouragement, nudges, your own reminders-to-self.

**Schema:**

```sql
CREATE TABLE message (
  id INTEGER PRIMARY KEY,
  body       TEXT NOT NULL,
  category   TEXT NOT NULL,      -- motivation|nudge|health|custom
  enabled    INTEGER NOT NULL DEFAULT 1,
  weight     REAL    NOT NULL DEFAULT 1.0,
  contexts   TEXT    NOT NULL,   -- JSON: ["idle","focus","break","drifting","session_end","day_start"]
  min_gap_min INTEGER NOT NULL DEFAULT 180,   -- per-message cooldown
  last_shown_at INTEGER, shown_count INTEGER NOT NULL DEFAULT 0,
  dismissed_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE message_rule (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  cadence_min INTEGER NOT NULL,       -- "roughly every N minutes"
  jitter_pct  INTEGER NOT NULL DEFAULT 25,
  max_per_day INTEGER NOT NULL DEFAULT 8,
  contexts    TEXT NOT NULL,
  quiet_hours TEXT                    -- JSON [["22:00","07:30"]]
);
CREATE TABLE message_event (
  id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES message(id),
  shown_at INTEGER NOT NULL, action TEXT   -- shown|dismissed|snoozed|acted
);
```

**`check_messages` (tick step 8):**

1. Determine current context from live state: `idle` (asleep) / `focus` / `break` / `drifting` / `session_end` / `day_start`.
2. Find enabled rules whose `contexts` include it, whose `cadence_min ± jitter` has elapsed, and whose `max_per_day` isn't spent.
3. Pick a message: filter by context and `min_gap_min`, then **weighted random without recent repeats** (no message twice in its own cooldown, and never the same one twice in a row).
4. Submit at priority 10 and let the arbiter say no. It will say no a lot. That is correct.

**Design constraints that keep this from becoming spam:**

- Default cadence is deliberately slow (90 min) and default `max_per_day` is 6. Ship it conservative.
- `drifting` context messages are **suppressed entirely** when a drift alert already fired in the last 15 min — you don't get nagged twice for the same drift.
- Dismissals are signal: a message dismissed 3 times without ever being "acted" gets auto-disabled with a note in the UI ("Izy muted this — it never lands"). Surface it, let you re-enable.
- **Templating:** `{task}`, `{intent}`, `{minutes_left}`, `{pomos_today}`, `{streak}`, `{name}`. Render with a strict whitelist — a missing variable renders the message unusable, so validate on save and refuse to store a template with unknown placeholders.
- Seed ~20 default messages across categories, all editable, all deletable. No hidden built-ins the user can't see or turn off.

**Editor UI requirements:** live preview with variables resolved against current state, a "test fire" button (bypasses cadence, still obeys the arbiter so you can't use it to prove a gate you've broken), per-message show/dismiss counts, bulk enable/disable by category, import/export as JSON or YAML so the library is version-controllable.

**Phase gate:** run a day at 10× clock; total messages shown ≤ `max_per_day`, none during deep-work streaks, none within 90 s of another interrupt, and `interrupt_log` explains every suppression.

---

## 6. Phase 6 — Dashboard (PySide6)

One window, sidebar navigation, subscribes to `/events` over the UDS and renders `/state`. **It holds no authoritative state** — every mutation is a POST/PATCH, and the UI updates when the next state delta arrives. If you find yourself keeping a local copy of the session to make the UI feel snappier, you've introduced the bug where the mascot and the dashboard disagree.

**Screens:**

- **Today** — mascot posture large, current session/pomodoro with ring, next 3 tasks by quadrant, a horizontal timeline of today's spans coloured on-task/off-task/AFK/break, on-task %, pomos completed. Clicking a span shows what tier classified it and why.
- **Tasks** — list view, inline create, filters, keyboard-first (`n` new, `e` edit, `space` complete, `1–4` set quadrant, `p` start pomodoro).
- **Matrix** — 2×2 drag-and-drop board. Quadrant headers carry counts and this-week time spent, so the matrix shows not just what you *labelled* important but what you actually spent hours on. The gap between those two numbers is the most useful thing in the app; show it explicitly ("Q2: 4 tasks, 40 min this week. Q3: 2 tasks, 6 h.").
- **Pomodoro** — big timer, config presets, current cycle, today's cycles.
- **Messages** — the library editor from Phase 5.
- **Reports** — day/week; on-task %, drift events, time by quadrant, time by app, pomos, interruption log with verdicts.
- **Settings** — quiet hours, deep-work threshold, drift threshold, tier-3 budget, widget on/off and appearance, autostart.

**Theming:** one `theme.py` with tokens (colour, radius, spacing, type scale), consumed by both the dashboard and the QML widget so they can't drift apart. Dark and light, following the GNOME preference via `gsettings get org.gnome.desktop.interface color-scheme`.

**Phase gate:** kill the daemon with the dashboard open → it shows a clear disconnected state and reconnects automatically when the daemon returns. No crash, no stale timer ticking on.

---

## 7. Phase 7 — The floating liquid-glass timer widget

A small always-on-top pill showing the pomodoro countdown, readable at a glance without switching windows.

**Window setup (QtQuick, separate process, `QT_QPA_PLATFORM=xcb`):**

```python
flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
# Qt.Tool keeps it out of the taskbar and the alt-tab list
view.setColor(Qt.transparent)
view.setFlags(flags)
view.setAttribute(Qt.WA_TranslucentBackground)
```

Launch it as its own process (`izy-widget`), supervised by the daemon or a user systemd unit. Crashing the widget must never touch tracking.

**The glass, since Mutter won't blur for us.** Build it in QML as stacked layers — this is the part to get right, because a flat translucent rectangle looks cheap and a well-composited one looks expensive:

1. **Sampled backdrop.** Read the wallpaper (`gsettings get org.gnome.desktop.background picture-uri-dark`), crop the rectangle under the widget's current position, blur it once with `MultiEffect { blurEnabled: true; blur: 1.0; blurMax: 48 }`, cache it. Re-sample on move, on wallpaper change, on monitor change. This is what actually sells the effect — real blurred content behind the glass, at near-zero cost because it's computed once, not per frame. Optionally upgrade to live content via the XDG screencast portal behind a setting, but **default to wallpaper sampling** — cheap, permissionless, no portal prompt.
2. **Tint** — `#FFFFFF` at 10 % (light) / `#0A0A0F` at 34 % (dark), over the blurred backdrop.
3. **Inner border** — 1 px gradient stroke, white 38 % at top-left → white 6 % at bottom-right. This single line does most of the "glass" work.
4. **Specular sweep** — a soft diagonal white gradient at 6 %, slowly animated on hover only.
5. **Noise** — a tiled 128×128 noise texture at 3 % opacity to kill gradient banding.
6. **Outer shadow** — `MultiEffect` drop shadow, 32 px blur, black 28 %, 8 px y-offset.
7. **Radius** — fully rounded ends on the compact pill (`height/2`), 28 px on the expanded card.

**Behaviour:**

- **Compact** (default, ~180×56): progress ring + `mm:ss` + one-letter phase.
- **Expanded on hover** (~280×140): task title, phase, pomo count `●●○○`, pause / skip / stop buttons. Animate with a 180 ms `OutCubic` transition; the width change should feel like one object, not two states.
- Drag to move; snaps to screen edges within 24 px; position persisted per monitor in config.
- Ring colour by phase: work `#5B8DEF`, short break `#3EC9A7`, long break `#9B7BEA`, paused `#8A8F98`, **drifting `#F0883E`** — same orange as the mascot's soft-alert, so the two always agree.
- Click-through mode (toggle): `setMask(QRegion())` on the input region so the widget is visible but not clickable.
- Opacity drops to 45 % when the pointer is within 80 px and the widget is not hovered, so it never fights with what's underneath.
- Auto-hide when no pomodoro is running (setting: hide / show idle pill / show next task).
- Reconnect loop to the UDS with backoff; if the daemon is gone, the ring greys and the widget shows `--:--` rather than a frozen number.

**Countdown accuracy:** the widget renders from `phase_ends_at` (absolute), interpolating locally at 60 fps and correcting against each 1 Hz state beat. Never count down from a local integer — you'll drift against the daemon and the numbers will disagree at the moment you're staring at them.

**Phase gate:** widget visible above a fullscreen browser, survives workspace switch, survives suspend/resume with the correct remaining time, CPU under 2 % idle, GPU memory stable over an hour.

---

## 8. Phase 8 (optional) — True live blur via a GNOME Shell extension

If the sampled-wallpaper glass isn't enough: a GNOME Shell extension (GJS) can place an actor in the shell's own layer and apply `Shell.BlurEffect` to real content behind it — genuine live liquid glass, true always-on-top, no XWayland. It would replace *only* the widget's window layer; the state still comes from the same UDS API over a small GJS client.

Cost: a second language, a second codebase, and breakage on every GNOME major release. Do it only after Phases 1–7 are stable and only if you're still bothered by the static backdrop.

---

## 9. Cross-cutting requirements

**Migrations.** Every schema change is a numbered, forward-only SQL file with a recorded version and a tested upgrade from the current production DB. Back up the DB before migrating. `izy doctor` reports schema version.

**Testing.** The tick loop must be testable with a fake clock and a fake watcher. Write golden tests that feed a scripted sequence of windows and timestamps and assert the full sequence of interrupts, classifications, mascot postures and DB rows. This is the only way to prove the arbiter works without sitting in front of it for a day. Every phase gate above becomes a test.

**Performance budget.** 1 Hz tick, daemon under 1 % CPU and 80 MB RSS idle. If a tick step needs I/O, it must be non-blocking or moved off the tick.

**Config.** One `~/.config/izy/config.toml`, schema-validated, hot-reloadable at tick step 1. The Settings screen writes it; hand-editing it keeps working.

**Logging.** Structured, `~/.local/state/izy/izy.log`, rotated. Every arbiter verdict and every tier-3 call logged with reason and cost.

---

## 10. Explicitly out of scope

- No second scheduler, no background threads with their own timers.
- No cloud sync, no accounts, no telemetry.
- No gamification — streaks, points, badges. Izy's job is to notice, not to score you.
- No blocking or blacklisting of apps or sites. The drift alert is the intervention.
- No AI features beyond the existing tier-3 classification and reminder parsing. The message library is a **store of your own text**, not a generator. Do not add "AI-generated motivation".
- No raising the notification rate to make the app feel more alive.

---

## 11. Order of work

```
Phase 1  control plane            ── blocks everything
Phase 2  interrupt arbiter        ── blocks 4, 5
Phase 3  tasks + Eisenhower       ── blocks 4 (task-backed pomodoros)
Phase 4  pomodoro                 ── blocks 7
Phase 5  message library
Phase 6  dashboard
Phase 7  glass widget
Phase 8  shell extension          ── optional
```

Stop at each phase gate. Show me the diff, the new tests, and the `interrupt_log` output before moving on.
