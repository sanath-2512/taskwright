"""Tools for the agent's own state: plan, working memory, knowledge, people, finishing."""

from __future__ import annotations

from ..human import Human
from ..memory import KnowledgeBase, RunState
from ..prompts import VERDICT_SCHEMA
from ..trace import Trace
from ..vault import Vault
from .base import INVALID, ToolResult, tool

PLAN_STATUSES = ["pending", "in_progress", "done", "blocked", "skipped"]


class KnowledgeToolkit:
    def __init__(self, kb: KnowledgeBase, trace: Trace, vault: Vault):
        self.kb, self.trace, self.vault = kb, trace, vault
        self._shown: set[str] = set()

    @tool("search_knowledge", "Search the company handbook (procedures, vendor directory, systems guide) and "
          "lessons from earlier runs.", {"query": {"type": "string"}}, ["query"])
    def search_knowledge(self, query: str) -> ToolResult:
        hits = self.kb.search(query, k=4)
        fresh = [(d, sc) for d, sc in hits if d.id not in self._shown]
        if hits and not fresh:
            self.trace.emit("knowledge_search", query=query, hits=[d.id for d, _ in hits])
            return ToolResult("No new information: the matching sections (" + ", ".join(d.id for d, _ in hits)
                              + ") were already shown to you earlier in this task, and the brief lists the "
                                "applicable procedures. Proceed with the task.")
        self._shown.update(d.id for d, _ in fresh)
        text = self.kb.render(fresh)
        if len(fresh) < len(hits):
            text += "\n\n(Also matching, already shown earlier: " + ", ".join(d.id for d, _ in hits if d not in [f for f, _ in fresh]) + ")"

        if self.kb.lessons:
            lessons = self.kb.lessons.relevant(query, k=3)
            if lessons:
                text += "\n\nLessons from earlier runs:\n" + "\n".join(f"- {l['lesson']}" for l in lessons)
        self.trace.emit("knowledge_search", query=query, hits=[d.id for d, _ in hits])
        return ToolResult(text, compact=f"[knowledge results for '{query}' elided; search again if needed]")

    @tool("list_credentials", "List the credentials you can use (names only) and where each may be used.")
    def list_credentials(self) -> ToolResult:
        return ToolResult(self.vault.describe())


class WorkerToolkit:
    def __init__(self, state: RunState, human: Human, trace: Trace):
        self.state, self.human, self.trace = state, human, trace

    @tool("remember", "Record a fact you will rely on (a value from a document, a record ID, a decision) with "
          "its source. Facts stay visible to you for the rest of the task and are checked by the verifier.",
          {"key": {"type": "string", "description": "Short name, e.g. total_amount"},
           "value": {"type": "string"},
           "source": {"type": "string", "description": "Where it came from, e.g. 'INV-1.pdf, Total Payable line'"}},
          ["key", "value", "source"])
    def remember(self, key: str, value: str, source: str) -> ToolResult:
        self.state.facts[key.strip()] = {"value": str(value), "source": source}
        self.trace.emit("fact", key=key, value=str(value), source=source)
        return ToolResult(f"Remembered {key} = {value}.")

    @tool("update_plan", "Replace the plan with an updated list of steps and statuses. Use when a step finishes or "
          "the plan needs to change.",
          {"steps": {"type": "array", "items": {"type": "object", "properties": {
              "step": {"type": "string"}, "status": {"type": "string", "enum": PLAN_STATUSES}},
              "required": ["step", "status"]}},
           "reason": {"type": "string", "description": "Why the plan changed (optional)"}},
          ["steps"])
    def update_plan(self, steps: list[dict], reason: str = "") -> ToolResult:
        clean = [{"step": str(s.get("step", "")).strip(),
                  "status": s.get("status") if s.get("status") in PLAN_STATUSES else "pending"}
                 for s in steps or [] if str(s.get("step", "")).strip()]
        if not clean:
            return ToolResult.error(INVALID, "The plan needs at least one step.")
        old = [p["step"] for p in self.state.plan]
        self.state.plan = clean
        self.trace.emit("plan", steps=clean, reason=reason, replanned=[p["step"] for p in clean] != old)
        return ToolResult("Plan updated.")

    @tool("ask_human", "Ask the requester a question when information is missing or a decision is outside your "
          "authority. Blocks until they answer. Do not use it for things you can find yourself.",
          {"question": {"type": "string"},
           "why": {"type": "string", "description": "Why you need this to proceed"},
           "options": {"type": "array", "items": {"type": "string"}, "description": "Suggested answers (optional)"}},
          ["question", "why"], idempotent=False)
    def ask_human(self, question: str, why: str, options: list[str] | None = None) -> ToolResult:
        self.trace.emit("question", question=question, why=why, options=options or [])
        answer = self.human.ask(question, options, why)
        self.state.clarifications.append({"question": question, "answer": answer})
        self.trace.emit("answer", question=question, answer=answer)
        return ToolResult(f"The requester answered: {answer}")

    @tool("finish", "Finish the task and hand it to independent verification. Call once the outcome is achieved "
          "or cannot be achieved.",
          {"status": {"type": "string", "enum": ["completed", "partial", "blocked", "failed"]},
           "summary": {"type": "string", "description": "Short message to the requester: what was done, key values, "
                                                         "anything they must do."},
           "claims": {"type": "array", "items": {"type": "string"},
                      "description": "Specific, checkable statements about the final state of the systems."},
           "follow_ups": {"type": "array", "items": {"type": "string"},
                          "description": "Actions for people (e.g. flag to security). Optional."}},
          ["status", "summary", "claims"], idempotent=False)
    def finish(self, status: str, summary: str, claims: list[str], follow_ups: list[str] | None = None) -> ToolResult:
        self.state.finished = {"status": status, "summary": summary, "claims": claims or [],
                               "follow_ups": follow_ups or []}
        self.trace.emit("finish", **self.state.finished)
        return ToolResult("Submitted for independent verification.")


class VerdictToolkit:
    def __init__(self, trace: Trace):
        self.trace = trace
        self.verdict: dict | None = None

    @tool("report_verdict", "Report your verification verdict. Call exactly once, at the end.",
          VERDICT_SCHEMA["properties"], VERDICT_SCHEMA["required"])
    def report_verdict(self, criteria: list[dict], side_effects: list[str], overall: str, summary: str) -> ToolResult:
        clean = []
        for c in criteria or []:      # be forgiving about shape; be strict about meaning
            if not isinstance(c, dict):
                c = {"criterion": str(c), "verdict": "unclear", "evidence": ""}
            v = str(c.get("verdict", "unclear")).lower()
            clean.append({"criterion": str(c.get("criterion", "")), "evidence": str(c.get("evidence", "")),
                          "verdict": v if v in ("pass", "fail", "unclear") else "unclear"})
        overall = str(overall).lower()
        self.verdict = {"criteria": clean,
                        "side_effects": [str(x) for x in (side_effects or []) if str(x).strip()],
                        "overall": overall if overall in ("verified", "failed", "unverified") else "unverified",
                        "summary": str(summary)}
        return ToolResult("Verdict recorded.")
