"""One run of the worker, end to end:

    Understand -> Plan -> (Execute -> Observe -> Adapt)* -> Finish
        -> Verify independently -> (fix and re-verify) -> Learn -> Report
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from . import prompts
from .config import ROOT, Settings
from .human import Human, ScriptedHuman
from .llm import LLM, LLMError
from .loop import AgentLoop
from .memory import KnowledgeBase, LessonStore, RunState
from .policy import PolicyEngine
from .report import write_report
from .tools.agent_tools import KnowledgeToolkit, VerdictToolkit, WorkerToolkit
from .tools.base import collect_tools
from .tools.browser import BrowserToolkit
from .tools.files import FileToolkit
from .trace import Trace
from .vault import Vault


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


class Run:
    def __init__(self, settings: Settings, task: str, llm: LLM, human: Human, listeners: list | None = None):
        self.s = settings
        self.task = task.strip()
        self.llm = llm
        self.human = human
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + _slug(task)
        self.dir = settings.runs_dir / self.run_id
        self.workspace = self.dir / "workspace"
        self.shots = self.dir / "screenshots"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace = Trace(self.dir / "trace.jsonl")
        for fn in listeners or []:
            self.trace.subscribe(fn)
        self.llm.on_retry = lambda msg: self.trace.emit("llm_retry", text=msg)
        self.vault = Vault.load(settings.vault_path)
        self.lessons = LessonStore(settings.memory_dir / "lessons.jsonl")
        self.kb = KnowledgeBase(settings.handbook_dir, settings.base_url, self.lessons)
        self.state = RunState(task=self.task)
        self.systems = (ROOT / "company" / "systems.md").read_text().replace("{{BASE_URL}}", settings.base_url.rstrip("/"))
        self.verdict: dict | None = None
        self.new_lessons: list[dict] = []
        self.t0 = time.time()

    # ------------------------------------------------------------------ public

    def execute(self) -> dict:
        self.trace.emit("run_start", run_id=self.run_id, task=self.task, provider=self.llm.provider,
                        model=self.llm.model, autonomy=self.s.autonomy or "config", run_dir=str(self.dir))
        error = None
        try:
            self._understand()
            self._execute_and_verify()
        except LLMError as e:
            error = str(e)
            self.trace.emit("error", text=error)
        except KeyboardInterrupt:
            error = "Interrupted by user."
            self.trace.emit("error", text=error)
        outcome = self.state.finished or {"status": "incomplete", "summary": error or "The worker stopped before "
                                          "finishing.", "claims": [], "follow_ups": []}
        if self.s.learn and not error:
            self._reflect()
        result = {
            "run_id": self.run_id, "task": self.task, "status": outcome["status"],
            "verification": (self.verdict or {}).get("overall", "not run"), "summary": outcome["summary"],
            "follow_ups": outcome.get("follow_ups", []), "steps": self.state.steps,
            "duration_s": round(time.time() - self.t0, 1), "usage": dict(self.llm.usage),
            "models": list(getattr(self.llm, "models_used", [self.llm.model])),
            "run_dir": str(self.dir), "error": error,
        }
        report = write_report(self.dir, self.trace.events, self.state, outcome, self.verdict, self.new_lessons, result)
        result["report"] = str(report)
        (self.dir / "result.json").write_text(json.dumps(result, indent=2))
        self.trace.emit("run_end", **result)
        return result

    # ------------------------------------------------------------------ understand + plan

    def _understand(self) -> None:
        hits = self.kb.search(self.task, k=6)
        self.state.lessons = self.lessons.relevant(self.task, k=6)
        self.task_knowledge = self.kb.pages_for(hits)
        prompt = prompts.understand_prompt(self.task, self.s.today_label,
                                           "Company systems:\n" + self.systems + "\n\n" + self.task_knowledge,
                                           self.state.lessons, self.vault.describe())
        self.trace.emit("phase", name="understand", sources=[d.id for d, _ in hits],
                        lessons=[l["lesson"] for l in self.state.lessons])
        brief = self.llm.structured(prompts.UNDERSTAND_SYSTEM.format(company=self.s.company), prompt,
                                    "submit_brief", "Submit the task brief.", prompts.BRIEF_SCHEMA)
        for k in ("success_criteria", "plan", "relevant_procedures", "risks", "blocking_questions"):
            v = brief.get(k)
            if not isinstance(v, list):
                v = [v] if v else []
            brief[k] = [x if isinstance(x, str) else str(next((y for y in x.values() if isinstance(y, str)), x))
                        if isinstance(x, dict) else str(x) for x in v]
        brief.setdefault("goal", self.task)
        brief.setdefault("interpretation", "")
        self.state.brief = brief
        self.state.plan = [{"step": s, "status": "pending"} for s in brief["plan"]]
        self.trace.emit("brief", **brief)
        for q in brief.get("blocking_questions", [])[:3]:
            self.trace.emit("question", question=q, why="Needed before starting", options=[])
            ans = self.human.ask(q, None, "Needed before starting")
            self.state.clarifications.append({"question": q, "answer": ans})
            self.trace.emit("answer", question=q, answer=ans)
        self.trace.emit("plan", steps=self.state.plan, reason="initial plan", replanned=False)

    # ------------------------------------------------------------------ execute + verify

    def _execute_and_verify(self) -> None:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not self.s.headed, slow_mo=self.s.slow_mo or 0)
            ctx = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
            try:
                worker = self._build_worker(ctx)
                done = worker.run(kickoff="Begin the task now. Work through the plan, adapting as you learn more.")
                if not done and not self.state.finished:
                    self.trace.emit("phase", name="incomplete", reason="step budget or stall")
                for rnd in range(1, self.s.verify_rounds + 1):
                    outcome = self.state.finished
                    if not outcome or (not outcome.get("claims") and outcome["status"] == "failed"):
                        break
                    self.verdict = self._verify(browser, ctx, outcome, rnd)
                    if self.verdict["overall"] == "verified" or rnd == self.s.verify_rounds:
                        break
                    if outcome["status"] in ("blocked", "failed") and self.verdict["overall"] != "failed":
                        break
                    feedback = self._feedback(self.verdict)
                    self.trace.emit("phase", name="rework", round=rnd, feedback=feedback)
                    self.state.finished = None
                    worker.messages.append({"role": "user", "content": feedback})
                    worker.run(extra_steps=20)
                    if not self.state.finished:
                        self.state.finished = outcome   # keep the last claims; the verdict stands
                        break
            finally:
                try:
                    ctx.close()
                    browser.close()
                except Exception:
                    pass

    def _build_worker(self, ctx) -> AgentLoop:
        policy = PolicyEngine.load(self.s.policies_path, autonomy=self.s.autonomy)
        self.trace.emit("phase", name="execute", autonomy=policy.autonomy)
        loop_ref: dict = {}
        browser_kit = BrowserToolkit(ctx, policy=policy, human=self.human, vault=self.vault, trace=self.trace,
                                     downloads_dir=self.workspace / "downloads", shots_dir=self.shots,
                                     base_url=self.s.base_url, label="agent", highlight=self.s.headed,
                                     intent_fn=lambda: (loop_ref["loop"].last_text if loop_ref else "")[:400])
        knowledge = KnowledgeToolkit(self.kb, self.trace, self.vault)
        shown_pages = set(re.findall(r'<source id="([^"#]+)"', getattr(self, "task_knowledge", "")))
        knowledge._shown.update(d.id for d in self.kb.sections if d.id.split("#")[0] in shown_pages)
        tools = collect_tools(browser_kit, FileToolkit(self.workspace, self.trace), knowledge,
                              WorkerToolkit(self.state, self.human, self.trace))
        # The handbook pages retrieved for this task are part of the (cached) static prompt, so the worker
        # starts with the procedures in front of it instead of spending turns searching for them.
        system = (prompts.EXECUTOR_SYSTEM.format(company=self.s.company)
                  + "\n\n# Company handbook pages relevant to this task (already retrieved for you)\n"
                  + getattr(self, "task_knowledge", ""))

        def set_steps(n):
            self.state.steps = n

        loop = AgentLoop(self.llm, tools, self.trace, self.vault, actor="agent",
                         system_fn=lambda: (system, prompts.state_block(self.state, self.s.today_label,
                                                                         loop.max_steps, self.systems)),
                         done_fn=lambda: self.state.finished is not None, max_steps=self.s.max_steps,
                         step_counter=set_steps)
        loop_ref["loop"] = loop
        return loop

    def _verify(self, browser, agent_ctx, outcome: dict, rnd: int) -> dict:
        self.trace.emit("phase", name="verify", round=rnd)
        vctx = browser.new_context(accept_downloads=True, storage_state=agent_ctx.storage_state(),
                                   viewport={"width": 1280, "height": 860})
        try:
            verdict_kit = VerdictToolkit(self.trace)
            files = FileToolkit(self.workspace, self.trace, read_only=True)
            bkit = BrowserToolkit(vctx, policy=PolicyEngine([], read_only=True), human=ScriptedHuman(approve=False),
                                  vault=self.vault, trace=self.trace, downloads_dir=self.workspace / "downloads",
                                  shots_dir=self.shots, base_url=self.s.base_url, label=f"verify{rnd}",
                                  highlight=self.s.headed)
            tools = collect_tools(bkit, files, KnowledgeToolkit(self.kb, self.trace, self.vault), verdict_kit,
                                  exclude={"write_file"})
            system = (prompts.VERIFIER_SYSTEM.format(company=self.s.company) + "\n\nCompany systems:\n" + self.systems
                      + "\n\n# Company handbook pages relevant to this task\n" + getattr(self, "task_knowledge", ""))
            loop = AgentLoop(self.llm, tools, self.trace, self.vault, actor="verifier", system_fn=lambda: system,
                             done_fn=lambda: verdict_kit.verdict is not None, max_steps=25)
            loop.run(kickoff=prompts.verifier_prompt(self.state, outcome, files.list_files().content,
                                                     self.s.today_label))
        finally:
            vctx.close()
        verdict = verdict_kit.verdict or {"criteria": [], "side_effects": [], "overall": "unverified",
                                          "summary": "The verifier did not reach a verdict within its budget."}
        # Never let a lenient verdict hide an explicit failure.
        if verdict["overall"] == "verified" and any(c.get("verdict") != "pass" for c in verdict["criteria"]):
            verdict["overall"] = "failed" if any(c.get("verdict") == "fail" for c in verdict["criteria"]) else "unverified"
        if verdict["overall"] == "verified" and verdict.get("side_effects"):
            verdict["overall"] = "failed"
        self.trace.emit("verdict", round=rnd, **verdict)
        return verdict

    @staticmethod
    def _feedback(verdict: dict) -> str:
        bad = [c for c in verdict["criteria"] if c.get("verdict") != "pass"]
        lines = [f"- [{c.get('verdict')}] {c.get('criterion')}: {c.get('evidence')}" for c in bad]
        lines += [f"- [side effect] {s}" for s in verdict.get("side_effects", [])]
        return ("An independent verifier checked the live systems and the task is NOT verified:\n" + "\n".join(lines)
                + "\n\nInvestigate the current state first (do not assume), fix what can be fixed without creating "
                  "duplicates, then call finish again with accurate claims. If something cannot be fixed, finish "
                  "with status partial and explain.")

    # ------------------------------------------------------------------ learn

    def _reflect(self) -> None:
        log = self._condensed_log()
        if not log.strip():
            return
        existing = "\n".join(f"- {l['lesson']}" for l in self.lessons.all()) or "(none)"
        prompt = (f"Task: {self.task}\n\nRun log (tool calls, failures, recoveries, human input, verification):\n"
                  f"{log}\n\nExisting lessons:\n{existing}\n\nCall submit_lessons.")
        try:
            out = self.llm.structured(prompts.REFLECT_SYSTEM.format(company=self.s.company), prompt,
                                      "submit_lessons", "Submit reusable lessons.", prompts.LESSONS_SCHEMA)
        except LLMError as e:
            self.trace.emit("error", text=f"Reflection failed: {e}")
            return
        self.new_lessons = self.lessons.add(out.get("lessons", [])[:4], self.run_id)
        self.trace.emit("lessons", added=self.new_lessons)

    def _condensed_log(self) -> str:
        lines, last_call = [], ""
        for e in self.trace.events:
            t = e["type"]
            if t == "tool_call" and e.get("actor") == "agent":
                args = json.dumps(e.get("args", {}), ensure_ascii=False)
                last_call = f"{e['name']}({args[:160]})"
            elif t == "tool_result" and e.get("actor") == "agent":
                mark = "ok" if e["ok"] else f"FAILED[{e.get('error_kind')}]"
                lines.append(f"{mark} {last_call} -> {e.get('summary', '')[:220]}")
            elif t == "retry":
                lines.append(f"runtime retry: {e.get('reason', '')[:150]}")
            elif t == "answer":
                lines.append(f"HUMAN Q: {e['question']} A: {e['answer']}")
            elif t == "approval_decision":
                lines.append(f"approval {e['rule']}: {'approved' if e['approved'] else 'DECLINED'} {e.get('note', '')}")
            elif t == "policy_block":
                lines.append(f"policy blocked: {e['rule']}")
            elif t == "verdict":
                lines.append(f"verification: {e['overall']} - {e.get('summary', '')[:300]}")
        if len(lines) > 140:
            lines = lines[:40] + ["..."] + lines[-100:]
        return "\n".join(lines)
