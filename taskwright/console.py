"""Live terminal view of a run, driven by trace events."""

from __future__ import annotations

import json
import os
import sys
import textwrap

if os.name == "nt":          # enable ANSI colours on Windows terminals
    os.system("")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
CODES = {"b": "1", "d": "2", "i": "3", "r": "31", "g": "32", "y": "33", "bl": "34", "m": "35", "c": "36", "gr": "90"}


def c(text: str, *styles: str) -> str:
    if not _COLOR or not styles:
        return text
    return "".join(f"\033[{CODES[s]}m" for s in styles) + text + "\033[0m"


def _short_args(name: str, args: dict) -> str:
    if name == "browser_open":
        return args.get("url", "")
    if name in ("browser_click",):
        return args.get("ref", "")
    if name == "browser_type":
        return f'{args.get("ref")} ← "{args.get("text")}"' + (" ⏎" if args.get("press_enter") else "")
    if name == "browser_fill_form":
        return ", ".join(f'{f.get("ref")}="{f.get("value")}"' for f in args.get("fields", []))[:160]
    if name == "remember":
        return f'{args.get("key")} = {args.get("value")}'
    if name in ("read_file", "write_file"):
        return args.get("path", "")
    if name == "search_knowledge":
        return f'"{args.get("query")}"'
    if name in ("finish", "update_plan", "ask_human", "report_verdict"):
        return ""
    s = json.dumps(args, ensure_ascii=False)
    return s[:140]


class ConsoleView:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.width = 100

    def p(self, s: str = "") -> None:
        print(s, flush=True)

    def wrap(self, text: str, indent: str, style: tuple = ()) -> None:
        for para in str(text).splitlines() or [""]:
            for line in textwrap.wrap(para, self.width - len(indent)) or [""]:
                self.p(indent + c(line, *style))

    def __call__(self, ev: dict) -> None:
        t = ev["type"]
        actor = ev.get("actor", "")
        v = actor == "verifier"
        tag = c("verifier ", "m") if v else ""
        if t == "run_start":
            self.p(c("━" * self.width, "gr"))
            self.p(c(" TASKWRIGHT ", "b") + c(f"· run {ev['run_id']} · {ev['provider']}/{ev['model']}", "gr"))
            self.wrap(ev["task"], " ", ("b",))
            self.p(c("━" * self.width, "gr"))
        elif t == "phase":
            names = {"understand": "UNDERSTAND", "execute": "EXECUTE", "verify": "VERIFY (independent, read-only)",
                     "rework": "REWORK after failed verification", "incomplete": "STOPPED"}
            self.p()
            self.p(c(f"◆ {names.get(ev['name'], ev['name'].upper())}", "b", "c")
                   + (c(f"  round {ev['round']}", "gr") if ev.get("round") else "")
                   + (c(f"  autonomy={ev['autonomy']}", "gr") if ev.get("autonomy") else ""))
            if ev["name"] == "understand":
                if ev.get("sources"):
                    self.p(c("  knowledge retrieved: " + ", ".join(ev["sources"]), "gr"))
                for l in ev.get("lessons", []):
                    self.wrap("lesson from memory: " + l, "  ", ("y",))
            if ev["name"] == "rework":
                self.wrap(ev.get("feedback", ""), "  ", ("y",))
        elif t == "brief":
            self.wrap("Goal: " + ev.get("goal", ""), "  ", ("b",))
            if ev.get("interpretation"):
                self.wrap("Interpretation: " + ev["interpretation"], "  ", ("d",))
            self.p(c("  Success criteria:", "b"))
            for i, x in enumerate(ev.get("success_criteria", []), 1):
                self.wrap(f"{i}. {x}", "    ")
            if ev.get("relevant_procedures"):
                self.p(c("  Procedures:", "b"))
                for x in ev["relevant_procedures"]:
                    self.wrap("• " + x, "    ", ("d",))
        elif t == "plan":
            if ev.get("reason") == "initial plan" or ev.get("replanned") or self.verbose:
                self.p(c("  Plan" + (" (revised)" if ev.get("replanned") else "") + ":", "b")
                       + (c(f"  {ev.get('reason')}", "gr") if ev.get("replanned") and ev.get("reason") else ""))
            marks = {"done": c("✓", "g"), "in_progress": c("▶", "c"), "blocked": c("!", "r"), "skipped": c("-", "gr"),
                     "pending": c("·", "gr")}
            if ev.get("reason") == "initial plan" or ev.get("replanned") or self.verbose:
                for i, s in enumerate(ev["steps"], 1):
                    self.wrap(f"{i}. {s['step']}", f"    {marks.get(s['status'], '·')} ")
            else:
                done = sum(1 for s in ev["steps"] if s["status"] == "done")
                cur = next((s["step"] for s in ev["steps"] if s["status"] == "in_progress"), "")
                self.p(c(f"  plan: {done}/{len(ev['steps'])} done", "gr") + (c(f" · now: {cur[:70]}", "gr") if cur else ""))
        elif t == "thought":
            self.wrap(ev["text"][:600], "  " + tag + c("│ ", "gr"), ("i", "d"))
        elif t == "tool_call":
            args = _short_args(ev["name"], ev.get("args", {}))
            self.p(f"  {tag}{c('▸', 'c')} {c(ev['name'], 'b')} {c(args, 'gr')}")
        elif t == "tool_result":
            if ev["ok"]:
                first = (ev.get("summary") or "").split(" / ")[0]
                self.p(f"    {c('✓', 'g')} {c(first[:110], 'gr')}")
                if ev.get("warning"):
                    self.wrap(ev["warning"], f"    {c('⚠', 'y')} ", ("y",))
            else:
                self.wrap(ev.get("summary", ""), f"    {c('✗', 'r')} ", ("r",))
        elif t in ("retry", "llm_retry"):
            self.p(f"    {c('↻', 'y')} {c(ev.get('reason') or ev.get('text', ''), 'y')}")
        elif t == "nudge":
            self.wrap(ev["text"], f"  {c('⚑ runtime:', 'y', 'b')} ", ("y",))
        elif t == "download":
            self.p(f"    {c('⤓', 'c')} {ev['file']}")
        elif t == "fact":
            self.p(f"    {c('✎', 'bl')} {c(ev['key'], 'b')} = {ev['value']} {c('(' + ev['source'] + ')', 'gr')}")
        elif t == "approval_reused":
            self.p(f"    {c('✓ identical payload already approved in this run, not asking again', 'g')}")
        elif t == "approval_decision":
            self.p(f"    {c('✓ approved', 'g', 'b') if ev['approved'] else c('✗ declined', 'r', 'b')}"
                   + (c(f" ({ev['note']})", 'gr') if ev.get("note") else ""))
        elif t == "policy_block":
            self.wrap(f"blocked by policy [{ev['rule']}]: {ev['reason']}", "    ⛔ ", ("r",))
        elif t == "evidence":
            self.p(f"    {c('📷 evidence:', 'm')} {ev['caption']}")
        elif t == "finish":
            self.p()
            self.p(c(f"■ WORKER FINISHED: {ev['status']}", "b"))
            for x in ev.get("claims", []):
                self.wrap("claim: " + x, "    ", ("d",))
        elif t == "verdict":
            col = {"verified": "g", "failed": "r"}.get(ev["overall"], "y")
            self.p(c(f"  verdict: {ev['overall'].upper()}", "b", col))
            for cr in ev.get("criteria", []):
                m = {"pass": c("✓", "g"), "fail": c("✗", "r")}.get(cr.get("verdict"), c("?", "y"))
                self.wrap(f"{cr.get('criterion')} — {cr.get('evidence')}", f"    {m} ")
            for s in ev.get("side_effects", []):
                self.wrap("side effect: " + s, "    ", ("r",))
        elif t == "lessons":
            if ev["added"]:
                self.p()
                self.p(c("◆ LEARN", "b", "c"))
                for l in ev["added"]:
                    self.wrap(l["lesson"], "  + ", ("y",))
        elif t == "stalled" or t == "budget_exhausted":
            self.p(c(f"  {t.replace('_', ' ')}", "r"))
        elif t == "error":
            self.wrap(ev["text"], "  ERROR ", ("r", "b"))
        elif t == "run_end":
            self.p()
            self.p(c("━" * self.width, "gr"))
            col = "g" if ev["verification"] == "verified" else ("r" if ev["verification"] == "failed" else "y")
            self.p(c(" OUTCOME ", "b") + c(f"{ev['status']} · verification: {ev['verification']}", "b", col)
                   + c(f" · {ev['steps']} steps · {ev['duration_s']}s · {ev['usage'].get('calls', 0)} LLM calls", "gr"))
            self.wrap(ev["summary"], " ")
            for f in ev.get("follow_ups", []):
                self.wrap("follow-up: " + f, " ", ("y",))
            self.p(c(f" report: {ev.get('report', '')}", "c"))
            self.p(c("━" * self.width, "gr"))
