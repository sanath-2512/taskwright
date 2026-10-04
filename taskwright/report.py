"""Self-contained HTML evidence report for a run (runs/<id>/report.html)."""

from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .memory import RunState

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#18202b;--mute:#667085;--line:#e4e7ec;--ok:#067647;--okbg:#ecfdf3;--bad:#b42318;
--badbg:#fef3f2;--warn:#b54708;--warnbg:#fffaeb;--acc:#3538cd;--accbg:#eef4ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e6e8ec;--mute:#98a2b3;--line:#2a2f3a;
--ok:#47cd89;--okbg:#0b2a1b;--bad:#f97066;--badbg:#2d1414;--warn:#fdb022;--warnbg:#2b2110;--acc:#8098f9;--accbg:#161b33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
main{max-width:1080px;margin:0 auto;padding:28px 18px 60px}h1{font-size:22px;margin:0 0 6px}h2{font-size:16px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:14px 0}
.mute{color:var(--mute)}.badge{display:inline-block;border-radius:999px;padding:2px 10px;font-weight:600;font-size:12px;margin-right:6px}
.ok{color:var(--ok);background:var(--okbg)}.bad{color:var(--bad);background:var(--badbg)}.warn{color:var(--warn);background:var(--warnbg)}
.acc{color:var(--acc);background:var(--accbg)}table{width:100%;border-collapse:collapse}td,th{border-bottom:1px solid var(--line);
padding:7px 8px;text-align:left;vertical-align:top}th{font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:.3px}
pre{white-space:pre-wrap;margin:0;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.tl{list-style:none;margin:0;padding:0}.tl li{border-left:3px solid var(--line);padding:6px 0 6px 14px;margin-left:6px}
.tl li.err{border-color:var(--bad)}.tl li.retry,.tl li.nudge{border-color:var(--warn)}.tl li.human{border-color:var(--acc)}
.tl li.verify{border-left-style:dashed}.tl .t{color:var(--mute);font-size:12px;margin-right:8px}code{font-size:12.5px}
.shots{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}.shots figure{margin:0}
.shots img{width:100%;border:1px solid var(--line);border-radius:6px}.shots figcaption{font-size:12px;color:var(--mute)}
details summary{cursor:pointer;color:var(--mute)}.thumb{max-width:220px;display:block;margin-top:6px;border:1px solid var(--line);border-radius:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.stat b{display:block;font-size:20px}
"""


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _badge(value: str) -> str:
    cls = {"completed": "ok", "verified": "ok", "pass": "ok", "partial": "warn", "blocked": "warn", "unverified": "warn",
           "unclear": "warn", "failed": "bad", "fail": "bad", "incomplete": "bad"}.get(value, "acc")
    return f'<span class="badge {cls}">{e(value)}</span>'


def _rel(run_dir: Path, path: str | None) -> str | None:
    if not path:
        return None
    local = run_dir / "screenshots" / Path(path).name     # robust to the run folder being moved
    if local.exists():
        return f"screenshots/{local.name}"
    try:
        return Path(os.path.relpath(path, run_dir)).as_posix()
    except ValueError:
        return None


def write_report(run_dir: Path, events: list[dict], state: RunState, outcome: dict, verdict: dict | None,
                 lessons: list[dict], result: dict) -> Path:
    b = state.brief or {}
    parts = []
    start = next((x for x in events if x["type"] == "run_start"), {})
    parts.append(f"""<div class="card"><div class="mute">Taskwright run {e(result['run_id'])}</div>
<h1>{e(state.task)}</h1>
<div>{_badge(outcome['status'])} {_badge((verdict or {}).get('overall', 'not verified'))}
<span class="mute">{e(start.get('provider'))} · {e(' → '.join(result.get('models') or [start.get('model', '')]))}</span></div></div>""")

    fu = "".join(f"<li>{e(x)}</li>" for x in outcome.get("follow_ups", []))
    parts.append(f"""<div class="card"><h2>Result for the requester</h2><p style="white-space:pre-wrap">{e(outcome.get('summary'))}</p>
{f'<h2 style="margin-top:14px">Follow-ups</h2><ul>{fu}</ul>' if fu else ''}</div>""")

    # criteria + verification
    rows = ""
    vcrit = (verdict or {}).get("criteria", [])
    for i, c in enumerate(b.get("success_criteria", [])):
        v = vcrit[i] if i < len(vcrit) else {}
        rows += f"<tr><td>{e(c)}</td><td>{_badge(v.get('verdict', 'n/a'))}</td><td>{e(v.get('evidence', ''))}</td></tr>"
    extra = [c for c in vcrit[len(b.get("success_criteria", [])):]]
    for v in extra:
        rows += f"<tr><td>{e(v.get('criterion'))}</td><td>{_badge(v.get('verdict', 'n/a'))}</td><td>{e(v.get('evidence'))}</td></tr>"
    se = "".join(f"<li>{e(s)}</li>" for s in (verdict or {}).get("side_effects", []))
    parts.append(f"""<div class="card"><h2>Independent verification</h2>
<p class="mute">{e((verdict or {}).get('summary', 'Verification did not run.'))}</p>
<table><tr><th>Success criterion</th><th>Verdict</th><th>Evidence</th></tr>{rows}</table>
{f'<h2 style="margin-top:14px">Side effects found</h2><ul>{se}</ul>' if se else ''}</div>""")

    procs = "".join(f"<li>{e(p)}</li>" for p in b.get("relevant_procedures", []))
    risks = "".join(f"<li>{e(p)}</li>" for p in b.get("risks", []))
    tracked = any(x["type"] == "plan" and x.get("reason") != "initial plan" for x in events)
    plan = "".join(f"<li>{e(p['step'])} {_badge(p.get('status', 'pending')) if tracked else ''}</li>" for p in state.plan)
    parts.append(f"""<div class="card"><h2>How the request was understood</h2>
<p><b>Goal.</b> {e(b.get('goal'))}</p><p><b>Interpretation.</b> {e(b.get('interpretation'))}</p>
<p><b>Procedures applied</b></p><ul>{procs}</ul><p><b>Risks anticipated</b></p><ul>{risks}</ul>
<p><b>Final plan</b></p><ol>{plan}</ol></div>""")

    facts = "".join(f"<tr><td><code>{e(k)}</code></td><td>{e(v['value'])}</td><td class='mute'>{e(v.get('source'))}</td></tr>"
                    for k, v in state.facts.items())
    if facts:
        parts.append(f'<div class="card"><h2>Facts recorded (working memory)</h2><table><tr><th>Key</th><th>Value</th>'
                     f'<th>Source</th></tr>{facts}</table></div>')

    human = []
    for x in events:
        if x["type"] == "approval_requested":
            human.append(f"<li><b>Approval requested</b> [{e(x['rule'])}] {e(x['reason'])}<pre>{e(x['action'])}</pre></li>")
        elif x["type"] == "approval_decision":
            human.append(f"<li>{_badge('approved' if x['approved'] else 'declined')} by {e(x.get('by'))} {e(x.get('note'))}</li>")
        elif x["type"] == "approval_reused":
            human.append(f"<li class='mute'>Earlier approval reused for an identical resubmission [{e(x['rule'])}]</li>")
        elif x["type"] == "answer":
            human.append(f"<li><b>Question:</b> {e(x['question'])}<br><b>Answer:</b> {e(x['answer'])}</li>")
        elif x["type"] == "policy_block":
            human.append(f"<li>{_badge('blocked')} [{e(x['rule'])}] {e(x['reason'])}</li>")
    if human:
        parts.append(f'<div class="card"><h2>Human in the loop and guardrails</h2><ul>{"".join(human)}</ul></div>')

    figs = []
    for x in events:
        if x["type"] == "evidence" and (rel := _rel(run_dir, x.get("screenshot"))):
            figs.append(f'<figure><a href="{e(rel)}"><img src="{e(rel)}" loading="lazy"></a>'
                        f'<figcaption>{e(x.get("by"))}: {e(x.get("caption"))}</figcaption></figure>')
    if figs:
        parts.append(f'<div class="card"><h2>Evidence</h2><div class="shots">{"".join(figs)}</div></div>')

    # timeline
    items = []
    for x in events:
        t = x["type"]
        actor = x.get("actor", "")
        cls = "verify" if actor == "verifier" else ""
        who = "verifier · " if actor == "verifier" else ""
        stamp = f'<span class="t">{x["t"]:>6.1f}s</span>'
        if t == "thought":
            items.append(f'<li class="{cls}"><details><summary>{stamp}{who}reasoning: {e(x["text"][:90])}…</summary>'
                         f'<pre>{e(x["text"])}</pre></details></li>')
        elif t == "tool_call":
            args = json.dumps(x.get("args", {}), ensure_ascii=False)
            items.append(f'<li class="{cls}">{stamp}{who}<code>{e(x["name"])}</code> <span class="mute">{e(args[:300])}</span></li>')
        elif t == "tool_result":
            rel = _rel(run_dir, x.get("screenshot"))
            img = f'<a href="{e(rel)}"><img class="thumb" src="{e(rel)}" loading="lazy"></a>' if rel else ""
            c = cls + ("" if x["ok"] else " err")
            mark = "✓" if x["ok"] else f"✗ {e(x.get('error_kind'))}"
            warn = f'<div style="color:var(--warn)">⚠ {e(x["warning"])}</div>' if x.get("warning") else ""
            if x.get("warning"):
                c += " retry"
            items.append(f'<li class="{c}">{stamp}{mark} <span class="mute">{e(x.get("summary"))}</span>{warn}{img}</li>')
        elif t in ("retry", "llm_retry"):
            items.append(f'<li class="retry">{stamp}↻ retry: {e(x.get("reason") or x.get("text"))}</li>')
        elif t == "nudge":
            items.append(f'<li class="nudge">{stamp}runtime nudge: {e(x["text"])}</li>')
        elif t in ("approval_requested", "approval_decision", "question", "answer", "policy_block"):
            label = {"approval_requested": "approval requested", "approval_decision": "approval " +
                     ("granted" if x.get("approved") else "declined"), "question": "question to human",
                     "answer": "human answered", "policy_block": "blocked by policy"}[t]
            items.append(f'<li class="human">{stamp}<b>{label}</b> {e(x.get("rule") or x.get("question") or "")} '
                         f'{e(x.get("answer", ""))}</li>')
        elif t == "phase":
            items.append(f'<li>{stamp}<b>phase: {e(x["name"])}</b></li>')
        elif t == "plan" and x.get("replanned"):
            items.append(f'<li>{stamp}<b>plan updated</b> {e(x.get("reason", ""))}</li>')
        elif t == "verdict":
            items.append(f'<li class="verify">{stamp}<b>verdict round {e(x["round"])}:</b> {_badge(x["overall"])} {e(x["summary"])}</li>')
    parts.append(f'<div class="card"><h2>Timeline</h2><ul class="tl">{"".join(items)}</ul></div>')

    if lessons:
        parts.append('<div class="card"><h2>Lessons saved to long-term memory</h2><ul>'
                     + "".join(f"<li>{e(l['lesson'])} <span class='mute'>({e(l['kind'])})</span></li>" for l in lessons)
                     + "</ul></div>")
    u = result.get("usage", {})
    parts.append(f"""<div class="card"><h2>Run statistics</h2><div class="grid">
<div class="stat"><b>{result['steps']}</b><span class="mute">worker tool steps</span></div>
<div class="stat"><b>{result['duration_s']}s</b><span class="mute">wall time</span></div>
<div class="stat"><b>{u.get('calls', 0)}</b><span class="mute">LLM calls</span></div>
<div class="stat"><b>{u.get('input_tokens', 0):,}</b><span class="mute">input tokens</span></div>
<div class="stat"><b>{u.get('output_tokens', 0):,}</b><span class="mute">output tokens</span></div></div>
<p class="mute">Full machine-readable log: trace.jsonl</p></div>""")

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Taskwright run report</title><style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>"""
    path = run_dir / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path


def regenerate(run_dir: Path) -> Path:
    """Rebuild report.html from a run's trace.jsonl (e.g. after changing the report's design)."""
    events = [json.loads(l) for l in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    first = lambda t: next((x for x in events if x["type"] == t), None)
    last = lambda t: next((x for x in reversed(events) if x["type"] == t), None)
    state = RunState(task=(first("run_start") or {}).get("task", ""))
    brief = first("brief") or {}
    state.brief = {k: v for k, v in brief.items() if k not in ("type", "t")}
    state.plan = (last("plan") or {}).get("steps", [])
    for x in events:
        if x["type"] == "fact":
            state.facts[x["key"]] = {"value": x["value"], "source": x.get("source")}
    fin = last("finish") or {}
    outcome = {k: fin.get(k) for k in ("status", "summary", "claims", "follow_ups")} if fin else \
        {"status": "incomplete", "summary": "", "claims": [], "follow_ups": []}
    v = last("verdict")
    verdict = {k: v.get(k) for k in ("criteria", "side_effects", "overall", "summary")} if v else None
    result = json.loads((run_dir / "result.json").read_text()) if (run_dir / "result.json").exists() else {}
    result.setdefault("run_id", run_dir.name)
    result.setdefault("steps", 0)
    result.setdefault("duration_s", 0)
    return write_report(run_dir, events, state, outcome, verdict, (last("lessons") or {}).get("added", []), result)
