# Implementation Prompt — Desktop Focus Companion

> Paste this into Claude Code at the root of an empty repo. Fill in the `{{...}}` slots first.
> Keep this file in the repo as `SPEC.md` — it is the source of truth for scope.

---

## Project

Build a local-first desktop focus companion: a tiny always-on-top mascot that tracks
whether I'm working on what I said I'd work on, holds reminders I give it in natural
language, and shows me an end-of-day retrospective of where my attention actually went.

Name: `Izy`
Primary user: me, one machine, one person. This is a personal tool, not a product.

---

## Step 0 — Environment probe (do this FIRST, write no application code yet)

The single biggest technical risk is whether the active window title is readable at all
on my system. Wayland does not expose active-window info to unprivileged clients, and the
usual fallbacks miss native Wayland windows.

Before anything else:

1. Detect and report: OS, distro, desktop environment, session type (`$XDG_SESSION_TYPE`),
   compositor, Python version.
2. Check whether ActivityWatch is installed and whether `aw-server` is running
   (`curl localhost:5600/api/0/info`).
3. Write a throwaway probe script that polls the active window title once per second for
   10 seconds across a few different apps, and run it. Confirm it returns real titles,
   not empty strings or `0x0`.
4. Check whether a frameless, transparent, always-on-top window can be created and
   positioned on this compositor. Some Wayland compositors refuse to let clients set
   their own position — if so, say so now, because the mascot placement design changes.

**Then stop and report findings before writing any application code.** If title tracking
is broken, we redesign around it rather than building on sand.

---

## Locked decisions — do not re-litigate these

- **Language:** Python 3.11+, single codebase. No Electron, no Tauri, no separate JS app.
- **UI:** PySide6. Frameless, translucent, always-on-top overlay for the mascot.
  Retrospective dashboard is a local HTML file opened in the browser, not a native GUI.
- **Storage:** one SQLite file at `~/.local/share/{{PROJECT_NAME}}/data.db`. Nothing leaves
  the machine except explicit LLM calls (below). No cloud sync, no accounts, no telemetry.
- **Activity source:** abstracted behind a `Watcher` protocol with two implementations —
  `ActivityWatchWatcher` (reads from the local `aw-server` REST API, preferred) and
  `NativeWatcher` (direct polling, fallback). Pick at runtime based on Step 0 findings.
- **LLM:** Anthropic API, key from `ANTHROPIC_API_KEY`. **All** LLM calls go through one
  module, `llm.py`, which enforces caching, rate limiting, and a hard daily call budget.
  No LLM call may be made from anywhere else in the codebase.
- **Packaging:** `uv` or `pip` + a `pyproject.toml`. Runs as a user systemd service (Linux)
  or equivalent. Must survive logout/login.

---

## Data model

```
sessions        id, started_at, ended_at, declared_intent, planned_minutes, outcome
activity_events id, session_id, ts, app, window_title, url, duration_s, afk
labels          id, event_id, source (rule|llm|user), on_task (bool), confidence, reason
reminders       id, created_at, raw_text, parsed_kind (time|context), due_at,
                trigger_context, status (pending|fired|done|dismissed|snoozed), fired_at
interventions   id, ts, kind, message, user_response (dismissed|acknowledged|snoozed)
```

`labels` is the training set. Every user correction is a labeled example — treat that
table as precious and never delete from it.

---

## Feature 1 — Session intent

The classifier cannot work without knowing what I'm supposed to be doing. So:

- Clicking the mascot opens a single-line input: **"What are you working on?"** plus a
  duration (default 25 min, Pomodoro-style, adjustable).
- That free-text intent is stored and becomes the reference point for every classification
  in the session: not *"is this productive in the abstract"* but *"is this plausibly
  related to `{{declared_intent}}`?"*
- Session end → short prompt: did you finish / partly / no. Store as `outcome`.
- A break timer runs between sessions. During breaks, classification is off entirely and
  the mascot never speaks except for due reminders.

---

## Feature 2 — Classification ladder

Evaluate in order, stop at the first confident answer. **Cost discipline is a hard
requirement, not an optimization.**

| Tier | Input | Cost | Notes |
|---|---|---|---|
| 1 | app + window title vs. user-defined allow/deny rules | free | handles the majority |
| 2 | browser tab URL (ActivityWatch web extension if present) | free | resolves most YouTube/Twitter ambiguity |
| 3 | LLM call: `(declared_intent, window_title, url)` → on_task + one-line reason | paid | **only** when tiers 1–2 are ambiguous |
| 4 | ask me, one tap: "Working on X? on-task / off-task" | free | when the LLM is below a confidence threshold |

Rules for Tier 3:
- Cache aggressively, keyed on `(intent_hash, app, normalized_title)`. Identical titles
  within a session must never trigger a second call.
- Hard cap on calls per hour and per day, configurable, defaulting low. On exceeding the
  budget, degrade to Tier 4, never silently to a guess.
- Batch: buffer ambiguous events for up to 60s and classify several in one call.
- Log every call with its token count so I can see actual spend in the dashboard.

**No screenshots in Phases 1–4.** Screen capture is Phase 5, opt-in, off by default, and
gated behind a per-app capture blocklist that is enforced *before* the capture happens,
not after.

---

## Feature 3 — Reminders

I want to be able to tell it things and have it hand them back at the right moment.

- Input: same box as the session intent, prefixed — e.g. `remind me to email the supervisor
  at 4pm`, `remind me in 20 minutes to check the training run`, `remind me next time I take
  a break to refill water`.
- Parsing: try `dateparser` first for plain absolute/relative times. Fall back to a single
  LLM call that returns strict JSON:
  `{kind: "time"|"context", due_at: iso8601|null, trigger_context: string|null, text: string}`.
  Never let the LLM invent a time that wasn't stated — if it's unclear, ask me.
- Context triggers to support at minimum: `on_break`, `session_start`, `session_end`,
  `app_opened:<name>`, `end_of_day`.
- Firing: mascot shows a small bubble with the reminder text and three actions —
  done / snooze 10m / dismiss. Never a modal, never focus-stealing, never a sound by default.
- Reminders fire even during a focus session, but only at the *next* natural boundary
  (session end or break) unless marked urgent when created. A reminder that breaks the
  focus it's supposed to protect is a bug.
- `list reminders` shows pending ones.

---

## Feature 4 — Mascot behaviour (read this section twice)

The mascot exists to be *available*, not to perform. The failure mode for this entire
project is that it becomes annoying and I close it permanently. Design against that:

**Visual**
- 48×56 px at 1x. Anchored to a screen corner, remembered across restarts.
- Click-through by default. Only becomes interactive on mouse hover.
- **No idle animation.** None. Motion in peripheral vision is exactly what steals
  attention. State is communicated by static posture and a subtle colour shift only.
- Three visual states: neutral (session running, on task) / soft-alert (drifting) /
  asleep (no session). Transitions cross-fade over ~400ms, no bouncing, no particles.
- Opacity drops to ~35% when the cursor is within 200px, so it never blocks anything.

**Interruption budget — enforce these as actual code, not guidelines**
- Maximum 3 unsolicited interruptions per hour, hard ceiling.
- Off-task must persist ≥ 4 continuous minutes before any drift alert. Brief context
  switches are normal work, not failure.
- After a dismissed alert: 15-minute cooldown before the next one.
- Never interrupt during a detected deep-work streak (≥ 20 min continuous on-task).
- Between 3 alerts and 0 alerts, prefer 0. Silent logging is always a valid outcome.

**What it says**
- Drift alerts reference *my own stated intent*, specifically:
  `"You said: fix the dataloader. YouTube, 11 min."` — no more than that.
- **No generic motivational quotes. None.** If encouragement appears at all, it must be
  derived from my own logged data ("4 clean sessions before noon, 3 days running"),
  and no more than once per day.
- No guilt language, no exclamation marks, no emoji in alert text.

---

## Feature 5 — Retrospective

A local HTML dashboard, regenerated on demand and automatically at end of day. This is
where the actual value lives — more than any real-time nagging.

Must show:
- Timeline of the day: on-task / off-task / break / AFK bands, hoverable to see the app.
- Where the drift started — which app pulled me out, and at what times of day I'm weakest.
- Session table: intent, planned vs. actual, outcome.
- Classification audit: every Tier 3 and 4 decision with its reason, and a one-click
  "this was wrong" that writes to `labels`. Correcting it must be effortless.
- LLM spend for the day.

---

## Non-goals — do not build these

- Website/app blocking or any enforcement. This tool observes and reports; it does not
  fight me.
- Multi-user, multi-device, sync, or accounts.
- Mobile.
- Gamification: XP, levels, streaks-as-pressure, leaderboards.
- Any UI that can steal keyboard focus.
- A settings GUI. A commented TOML config file is sufficient.

---

## Phases — implement Phase 1 only, then stop

**Phase 1 — Skeleton + logging (build this now)**
Watcher abstraction with both adapters, SQLite schema, session start/stop, activity event
logging, an hourly "were you on task?" self-label prompt, systemd service, CLI to dump
the day's log. Mascot is a static placeholder square. No LLM calls at all.
*Done when:* it runs for a full day unattended, survives a reboot, and the day's events
are queryable.

**Phase 2 — Reminders**
Full reminder feature above. Valuable immediately and independent of the classifier, so it
ships while Phase 1 data accumulates.
*Done when:* time and context reminders both fire correctly, including across a restart.

**Phase 3 — Classification ladder**
Tiers 1–4, caching, budget enforcement.
*Done when:* a day of events is classified with fewer than `50` LLM calls and I agree
with ≥ 80% of the labels on review.

**Phase 4 — Retrospective dashboard**

**Phase 5 — Mascot art, states, Pomodoro polish, optional screen capture tier**

---

## Working agreement

- Do Step 0 and report before writing application code.
- Implement one phase at a time. Stop at the end of each phase and wait for me.
- Ask before any decision that would be expensive to reverse (schema shape, watcher
  interface, threading model). Don't guess and don't silently pick.
- Keep modules under ~300 lines. If a file is growing past that, tell me and propose a split.
- Every LLM call path needs a test that runs with the API mocked. I should be able to run
  the whole test suite offline with no key set.
- Write the config file with comments explaining every knob, especially the interruption
  budget numbers — those are the ones I'll actually tune.
