"""DayReport -> a single self-contained HTML file.

No CDN, no fonts, no network of any kind: this is a local-first tool and the
page must render with the machine offline. Everything is inline.

The palette is the validated categorical default (blue / orange / aqua), run
through the data-viz validator for both surfaces. AFK is deliberately neutral
gray rather than a fourth hue — being away is an absence of activity, not a
category of it, and keeping it gray leaves three real hues, which clears the
all-pairs CVD floors. Aqua sits below 3:1 on the light surface, so the relief
rule applies and identity is never carried by colour alone: every band kind is
also named in the legend and repeated in the totals table.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta

from ..models import from_iso
from .data import AFK, BREAK, OFF, ON, UNJUDGED, BREAKS_ARE_DERIVED, DayReport

KIND_LABEL = {ON: "On task", OFF: "Off task", BREAK: "Break",
              AFK: "Away", UNJUDGED: "Not judged"}
KIND_ORDER = (ON, OFF, BREAK, AFK, UNJUDGED)

_CSS = """
:root {
  color-scheme: light;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --border: #dedcd6;
  --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #77756e;
  --on: #2a78d6; --off: #eb6834; --break: #1baf7a; --afk: #b6b4ac;
  --unjudged: #dcdad3;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-0: #111110; --surface-1: #1a1a19; --border: #34342f;
    --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #8f8d83;
    --on: #3987e5; --off: #d95926; --break: #199e70; --afk: #5c5a53;
    --unjudged: #38372f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px; background: var(--surface-0);
  color: var(--text-primary); font: 14px/1.5 system-ui, -apple-system, sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 2px; letter-spacing: -0.01em; }
h2 { font-size: 13px; font-weight: 600; margin: 0 0 12px; color: var(--text-secondary);
     text-transform: uppercase; letter-spacing: 0.06em; }
.sub { color: var(--text-muted); margin: 0 0 28px; font-size: 13px; }
section { background: var(--surface-1); border: 1px solid var(--border);
          border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }
.hero { display: flex; gap: 36px; flex-wrap: wrap; align-items: baseline; }
.hero .n { font-size: 30px; font-weight: 600; letter-spacing: -0.02em; }
.hero .k { color: var(--text-muted); font-size: 12px; text-transform: uppercase;
           letter-spacing: 0.06em; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; margin: 0 0 10px;
          font-size: 12px; color: var(--text-secondary); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.timeline { width: 100%; height: 46px; display: block; }
.axis { display: flex; justify-content: space-between; color: var(--text-muted);
        font-size: 11px; margin-top: 6px; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--text-muted); font-size: 11px;
     text-transform: uppercase; letter-spacing: 0.05em; padding: 0 10px 8px 0;
     border-bottom: 1px solid var(--border); }
td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--border);
     vertical-align: top; }
tr:last-child td { border-bottom: none; }
.num { font-variant-numeric: tabular-nums; white-space: nowrap; }
.muted { color: var(--text-muted); }
.bar { height: 9px; border-radius: 4px; background: var(--off); min-width: 2px; }
.bartrack { background: var(--unjudged); border-radius: 4px; height: 9px; width: 100%; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px;
       font-size: 11px; border: 1px solid var(--border); color: var(--text-secondary); }
button.fix { font: inherit; font-size: 12px; padding: 3px 9px; cursor: pointer;
             border: 1px solid var(--border); border-radius: 6px;
             background: transparent; color: var(--text-secondary); }
button.fix:hover { border-color: var(--text-muted); color: var(--text-primary); }
button.fix[disabled] { opacity: .45; cursor: default; }
#tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
       background: var(--surface-1); border: 1px solid var(--border);
       border-radius: 8px; padding: 7px 10px; font-size: 12px; max-width: 340px;
       box-shadow: 0 4px 16px rgba(0,0,0,.16); z-index: 20; }
.empty { color: var(--text-muted); font-style: italic; }
.note { color: var(--text-muted); font-size: 12px; margin-top: 10px; }
"""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def fmt_duration(seconds: float) -> str:
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def _clock(dt) -> str:
    return dt.astimezone().strftime("%H:%M")


def render(report: DayReport) -> str:
    body = [
        f"<h1>{_esc(report.day.strftime('%A %-d %B %Y'))}</h1>",
        f"<p class='sub'>Where your attention actually went.</p>",
        _summary(report), _timeline(report), _sessions(report),
        _drift(report), _hours(report), _audit(report), _spend(report),
    ]
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Izy — {_esc(report.day.date().isoformat())}</title>"
            f"<style>{_CSS}</style></head><body><div class='wrap'>"
            + "".join(body) +
            "</div><div id='tip'></div><script>" + _JS + "</script></body></html>")


def _summary(r: DayReport) -> str:
    share = r.on_task_share
    share_text = f"{share * 100:.0f}%" if share is not None else "—"
    cells = [("On task", fmt_duration(r.totals.get(ON, 0))),
             ("Off task", fmt_duration(r.totals.get(OFF, 0))),
             ("Share on task", share_text),
             ("Away", fmt_duration(r.totals.get(AFK, 0)))]
    inner = "".join(f"<div><div class='n'>{_esc(v)}</div>"
                    f"<div class='k'>{_esc(k)}</div></div>" for k, v in cells)
    # The totals table is also the relief for the light-surface contrast WARN:
    # every band kind is named here in text, not only shown as colour.
    rows = "".join(
        f"<tr><td><span class='swatch' style='background:var(--{k})'></span> "
        f"{_esc(KIND_LABEL[k])}</td>"
        f"<td class='num'>{fmt_duration(r.totals.get(k, 0))}</td></tr>"
        for k in KIND_ORDER if r.totals.get(k, 0) > 0)
    return (f"<section><div class='hero'>{inner}</div>"
            f"<table style='margin-top:18px'><thead><tr><th>Band</th>"
            f"<th>Total</th></tr></thead><tbody>{rows}</tbody></table></section>")


def _timeline(r: DayReport) -> str:
    if not r.bands:
        return "<section><h2>Timeline</h2><p class='empty'>No activity recorded.</p></section>"
    start = min(b.start for b in r.bands)
    end = max(b.end for b in r.bands)
    span = max(1.0, (end - start).total_seconds())

    # User units, not percentages: the viewBox is 1000 wide, so one unit is
    # ~1px at full width and the 2-unit gap below is the mark spec's 2px
    # surface gap between adjacent fills. Percentages cannot express it.
    parts = []
    for band in r.bands:
        x = (band.start - start).total_seconds() / span * 1000
        w = max(1.0, band.seconds / span * 1000 - 2)
        payload = _esc(json.dumps({
            "kind": KIND_LABEL[band.kind],
            "app": band.app or "",
            "title": (band.title or "")[:90],
            "from": _clock(band.start), "to": _clock(band.end),
            "dur": fmt_duration(band.seconds)}))
        # 2px surface gap between fills, per the mark spec.
        parts.append(
            f"<rect x='{x:.2f}' y='0' width='{w:.2f}' height='46' rx='2' "
            f"fill='var(--{band.kind})' data-tip='{payload}'>"
            f"<title>{_esc(KIND_LABEL[band.kind])} — {_esc(band.app or '')} "
            f"{_clock(band.start)}–{_clock(band.end)}</title></rect>")

    legend = "".join(
        f"<span><i class='swatch' style='background:var(--{k})'></i>"
        f"{_esc(KIND_LABEL[k])}</span>"
        for k in KIND_ORDER if r.totals.get(k, 0) > 0)
    return (f"<section><h2>Timeline</h2><div class='legend'>{legend}</div>"
            f"<svg class='timeline' viewBox='0 0 1000 46' preserveAspectRatio='none' "
            f"role='img' aria-label='Activity through the day'>{''.join(parts)}</svg>"
            f"<div class='axis'><span>{_clock(start)}</span>"
            f"<span>{_clock(end)}</span></div>"
            f"<p class='note'>{_esc(BREAKS_ARE_DERIVED)}</p></section>")


def _sessions(r: DayReport) -> str:
    if not r.sessions:
        return ("<section><h2>Sessions</h2>"
                "<p class='empty'>No sessions today.</p></section>")
    rows = []
    for s in r.sessions:
        actual = ((s.ended_at - s.started_at).total_seconds() / 60
                  if s.ended_at else None)
        actual_text = f"{actual:.0f}m" if actual is not None else "open"
        rows.append(
            f"<tr><td class='num muted'>{_clock(s.started_at)}</td>"
            f"<td>{_esc(s.declared_intent)}</td>"
            f"<td class='num'>{s.planned_minutes}m</td>"
            f"<td class='num'>{_esc(actual_text)}</td>"
            f"<td><span class='tag'>{_esc(s.outcome or 'unanswered')}</span></td></tr>")
    return (f"<section><h2>Sessions</h2><table><thead><tr><th>Start</th>"
            f"<th>Intent</th><th>Planned</th><th>Actual</th><th>Outcome</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>")


def _drift(r: DayReport) -> str:
    if not r.drift_starts:
        return ("<section><h2>What pulled you out</h2>"
                "<p class='empty'>No drift recorded today.</p></section>")
    top = max(secs for _, _, secs in r.drift_starts) or 1.0
    rows = "".join(
        f"<tr><td>{_esc(app)}</td>"
        f"<td class='num'>{count}×</td>"
        f"<td class='num'>{fmt_duration(secs)}</td>"
        f"<td style='width:50%'><div class='bartrack'>"
        f"<div class='bar' style='width:{secs / top * 100:.1f}%'></div></div></td></tr>"
        for app, count, secs in r.drift_starts[:8])
    return (f"<section><h2>What pulled you out</h2>"
            f"<table><thead><tr><th>App</th><th>Times</th><th>Time lost</th>"
            f"<th></th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p class='note'>Bars are the off-task time that followed, not the "
            f"number of times — that is the part that varies.</p></section>")


def _hours(r: DayReport) -> str:
    if not r.weakest_hours:
        return ("<section><h2>When you are weakest</h2>"
                "<p class='empty'>Not enough judged activity yet.</p></section>")
    rows = "".join(
        f"<tr><td class='num'>{hour:02d}:00</td>"
        f"<td class='num'>{share * 100:.0f}% off</td>"
        f"<td style='width:55%'><div class='bartrack'>"
        f"<div class='bar' style='width:{share * 100:.1f}%'></div></div></td>"
        f"<td class='num muted'>{fmt_duration(judged)} judged</td></tr>"
        for hour, share, judged in r.weakest_hours)
    return (f"<section><h2>When you are weakest</h2>"
            f"<table><thead><tr><th>Hour</th><th>Off task</th><th></th>"
            f"<th>Sample</th></tr></thead><tbody>{rows}</tbody></table>"
            f"<p class='note'>Sample size is shown because one bad ten-minute "
            f"hour is not a pattern.</p></section>")


def _audit(r: DayReport) -> str:
    if not r.audit:
        return ("<section><h2>Classification audit</h2>"
                "<p class='empty'>No tier 3 or tier 4 decisions today — "
                "everything was settled by the free rules.</p></section>")
    rows = []
    for a in r.audit:
        verdict = "on task" if a["on_task"] else "off task"
        confidence = (f"{a['confidence']:.2f}" if a["confidence"] is not None else "—")
        rows.append(
            f"<tr data-event='{a['event_id']}' data-on='{1 if a['on_task'] else 0}'>"
            f"<td class='num muted'>{_clock(from_iso(a['created_at']))}</td>"
            f"<td>{_esc((a['window_title'] or a['app'] or '')[:52])}</td>"
            f"<td><span class='tag'>{_esc(verdict)}</span></td>"
            f"<td class='num muted'>{_esc(confidence)}</td>"
            f"<td class='muted'>{_esc(a['source'])}</td>"
            f"<td class='muted'>{_esc((a['reason'] or '')[:60])}</td>"
            f"<td><button class='fix' data-event='{a['event_id']}' "
            f"data-to='{0 if a['on_task'] else 1}'>This was wrong</button></td></tr>")
    return (f"<section><h2>Classification audit</h2>"
            f"<table><thead><tr><th>Time</th><th>Window</th><th>Verdict</th>"
            f"<th>Conf.</th><th>By</th><th>Reason</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
            f"<p class='note' id='fixnote'>Every correction is a labelled "
            f"example and is kept forever.</p></section>")


def _spend(r: DayReport) -> str:
    llm = r.llm
    cost = llm.get("cost_usd", 0.0)
    return (f"<section><h2>LLM spend</h2><div class='hero'>"
            f"<div><div class='n'>${cost:.4f}</div><div class='k'>today</div></div>"
            f"<div><div class='n'>{llm.get('calls', 0)}</div>"
            f"<div class='k'>paid calls</div></div>"
            f"<div><div class='n'>{llm.get('cache_hits', 0)}</div>"
            f"<div class='k'>cache hits</div></div>"
            f"<div><div class='n'>{llm.get('input_tokens', 0):,}</div>"
            f"<div class='k'>input tokens</div></div>"
            f"<div><div class='n'>{llm.get('output_tokens', 0):,}</div>"
            f"<div class='k'>output tokens</div></div></div></section>")


_JS = """
const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('mousemove', e => {
    const d = JSON.parse(el.dataset.tip);
    tip.innerHTML = `<b>${d.kind}</b> · ${d.dur}<br>${d.from}–${d.to}` +
      (d.app ? `<br>${d.app}` : '') + (d.title ? `<br><span>${d.title}</span>` : '');
    tip.style.opacity = 1;
    const x = Math.min(e.clientX + 14, window.innerWidth - 350);
    tip.style.left = x + 'px';
    tip.style.top = (e.clientY + 16) + 'px';
  });
  el.addEventListener('mouseleave', () => { tip.style.opacity = 0; });
});

// "This was wrong" posts to the local server izy report starts. Opened as a
// plain file:// there is no server to talk to, so the button explains how to
// do it from the shell instead of failing silently.
document.querySelectorAll('button.fix').forEach(btn => {
  btn.addEventListener('click', async () => {
    const id = btn.dataset.event, to = btn.dataset.to === '1';
    btn.disabled = true; btn.textContent = 'saving…';
    try {
      const res = await fetch('/relabel', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({event_id: Number(id), on_task: to}),
      });
      if (!res.ok) throw new Error(await res.text());
      btn.textContent = to ? 'now on task' : 'now off task';
      const row = btn.closest('tr');
      if (row) row.querySelector('.tag').textContent = to ? 'on task' : 'off task';
    } catch (err) {
      btn.disabled = false; btn.textContent = 'This was wrong';
      document.getElementById('fixnote').textContent =
        `Not served by izy report, so this page cannot write. Run:  izy relabel ${id} ${to ? 'on' : 'off'}`;
    }
  });
});
"""
