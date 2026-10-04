"""The observe-think-act loop shared by the worker and the verifier.

Responsibilities that live here rather than in the prompt:
  * dispatching tool calls with argument validation,
  * automatic retry of *idempotent* tools on transient failures,
  * stopping a batch of tool calls at the first failure (the rest were planned
    against a world that no longer exists),
  * loop/stall detection with a corrective nudge,
  * context compaction (old page snapshots are elided; working memory is
    re-injected every turn instead),
  * a hard step budget.
"""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from typing import Callable

from .llm import LLM, LLMError, ToolCall
from .tools.base import FAILED, INVALID, TRANSIENT, ToolResult, ToolSpec
from .trace import Trace
from .vault import Vault

KEEP_FULL_HEAVY_RESULTS = 2   # the most recent N large tool results stay verbatim


class AgentLoop:
    def __init__(self, llm: LLM, tools: dict[str, ToolSpec], trace: Trace, vault: Vault, *, actor: str,
                 system_fn: Callable[[], str], done_fn: Callable[[], bool], max_steps: int,
                 step_counter: Callable[[int], None] | None = None):
        self.llm, self.tools, self.trace, self.vault = llm, tools, trace, vault
        self.actor = actor
        self.system_fn = system_fn
        self.done_fn = done_fn
        self.max_steps = max_steps
        self.steps = 0
        self.step_counter = step_counter or (lambda n: None)
        self.messages: list[dict] = []
        self.last_text = ""
        self._history: list[tuple[str, str]] = []   # (call signature, result hash)
        self.keep_heavy = KEEP_FULL_HEAVY_RESULTS
        self._consecutive_errors = 0

    # ------------------------------------------------------------------ main loop

    def run(self, kickoff: str | None = None, extra_steps: int = 0) -> bool:
        """Run until done_fn() or the budget is spent. Returns True if done."""
        self.max_steps += extra_steps
        if kickoff:
            self.messages.append({"role": "user", "content": kickoff})
        idle_turns = 0
        while self.steps < self.max_steps:
            try:
                resp = self.llm.complete(self.system_fn(), self._compacted(), list(self.tools.values()))
            except LLMError as e:
                # A context that outgrew the provider's per-request limit: compact harder and try once more.
                if self.keep_heavy > 0 and any(x in str(e).lower() for x in ("too large", "413", "context length",
                                                                               "maximum context", "tokens per minute")):
                    self.keep_heavy -= 1
                    self.trace.emit("nudge", actor=self.actor, text=f"Context too large for the model; keeping only "
                                                                     f"{self.keep_heavy} full page(s) in context.")
                    continue
                raise
            self.trace.emit("llm", actor=self.actor, usage=resp.usage, tool_calls=[c.name for c in resp.tool_calls])
            self.last_text = resp.text          # this turn's stated intent (shown on approval requests)
            if resp.text:
                self.trace.emit("thought", actor=self.actor, text=resp.text)
            self.messages.append({"role": "assistant", "content": resp.text,
                                  "tool_calls": [{"id": c.id, "name": c.name, "args": c.args, "extra": c.extra}
                                                 for c in resp.tool_calls]})
            if not resp.tool_calls:
                idle_turns += 1
                if idle_turns >= 3:
                    self.trace.emit("stalled", actor=self.actor, reason="model stopped calling tools")
                    return False
                self.messages.append({"role": "user", "content":
                                      "Continue the task by calling tools. If the work is finished or cannot "
                                      "continue, call the finishing tool."})
                continue
            idle_turns = 0

            results, failed = [], False
            for call in resp.tool_calls:
                if failed or self.done_fn():
                    results.append({"id": call.id, "name": call.name, "is_error": True,
                                    "content": "SKIPPED: an earlier action in this turn failed or the task was "
                                               "already finished. Re-check the current state before continuing.",
                                    "compact": None})
                    continue
                self.steps += 1
                self.step_counter(self.steps)
                res = self.dispatch(call)
                results.append({"id": call.id, "name": call.name, "content": res.content,
                                "compact": res.compact, "is_error": res.is_error})
                if res.is_error:
                    failed = True
            note = self._stall_check(resp.tool_calls, results)
            self.messages.append({"role": "tool", "results": results, "note": note})
            if self.done_fn():
                return True
        self.trace.emit("budget_exhausted", actor=self.actor, steps=self.steps)
        return False

    # ------------------------------------------------------------------ tool dispatch

    def dispatch(self, call: ToolCall) -> ToolResult:
        spec = self.tools.get(call.name)
        if not spec:
            return ToolResult.error(INVALID, f"Unknown tool '{call.name}'. Available: {', '.join(self.tools)}")
        if call.args_error:
            return ToolResult.error(INVALID, f"Arguments for {call.name} could not be parsed ({call.args_error}).")
        props = spec.input_schema.get("properties", {})
        missing = [r for r in spec.input_schema.get("required", []) if r not in call.args]
        if missing:
            return ToolResult.error(INVALID, f"{call.name} is missing required arguments: {', '.join(missing)}.")
        args = {k: v for k, v in call.args.items() if k in props}
        self.trace.emit("tool_call", actor=self.actor, step=self.steps, name=call.name,
                        args=json.loads(self.vault.redact(json.dumps(args, ensure_ascii=False))))
        t0 = time.time()
        attempt = 0
        while True:
            try:
                res = spec.fn(**args)
            except Exception as e:  # a tool bug must not kill the run; the agent can work around it
                self.trace.emit("tool_exception", actor=self.actor, name=call.name, error=traceback.format_exc()[-1500:])
                res = ToolResult.error(FAILED, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
            if res.error_kind == TRANSIENT and spec.idempotent and attempt < 2:
                attempt += 1
                self.trace.emit("retry", actor=self.actor, tool=call.name, attempt=attempt, reason=res.content[:200])
                time.sleep(1.5 * attempt)
                continue
            break
        res.content = self.vault.redact(res.content)
        if res.compact:
            res.compact = self.vault.redact(res.compact)
        self.trace.emit("tool_result", actor=self.actor, step=self.steps, name=call.name, ok=not res.is_error,
                        error_kind=res.error_kind, summary=_summarize(res.content),
                        screenshot=res.data.get("screenshot"), url=res.data.get("url"),
                        warning=res.data.get("warning") or None,
                        duration=round(time.time() - t0, 2))
        return res

    # ------------------------------------------------------------------ stall detection

    def _stall_check(self, calls: list[ToolCall], results: list[dict]) -> str | None:
        notes = []
        for call, r in zip(calls, results):
            sig = call.name + json.dumps(call.args, sort_keys=True)
            self._history.append((sig, hashlib.md5(r["content"].encode()).hexdigest()))
            self._consecutive_errors = self._consecutive_errors + 1 if r["is_error"] else 0
        recent = self._history[-6:]
        if recent and recent.count(recent[-1]) >= 3:
            notes.append("You have now made the same call with the same result 3 times. Stop repeating it: "
                         "re-read the page, try a different approach, consult the knowledge base, or ask the human.")
        if self._consecutive_errors >= 4:
            notes.append(f"Your last {self._consecutive_errors} actions failed. Step back and re-plan: check where "
                         "you are, what the errors say, and whether you need information from the knowledge base "
                         "or the human.")
        if self.max_steps - self.steps in (5, 6) and not self.done_fn():
            notes.append("Only a few steps of budget remain. Wrap up and call the finishing tool.")
        if notes:
            self.trace.emit("nudge", actor=self.actor, text=" ".join(notes))
            return "[runtime] " + " ".join(notes)
        return None

    # ------------------------------------------------------------------ context management

    def _compacted(self) -> list[dict]:
        heavy_seen = 0
        out = []
        for m in reversed(self.messages):
            if m["role"] != "tool":
                out.append(m)
                continue
            new_results = []
            for r in reversed(m["results"]):
                content = r["content"]
                if len(content) > 1500:
                    heavy_seen += 1
                    if heavy_seen > self.keep_heavy:
                        content = r.get("compact") or (content[:400] + "\n…[older result elided]")
                new_results.append({"id": r["id"], "name": r["name"], "content": content, "is_error": r["is_error"]})
            out.append({"role": "tool", "results": list(reversed(new_results)), "note": m.get("note")})
        return list(reversed(out))


def _summarize(content: str) -> str:
    lines = [l for l in content.splitlines() if l.strip()]
    head = " / ".join(lines[:2])
    return head[:300]
