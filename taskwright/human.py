"""How the agent reaches a human: clarifying questions and approvals.

ConsoleHuman is used for interactive runs. ScriptedHuman answers from a
script (tests, evals). UnattendedHuman is for runs nobody is watching: it
denies every approval and answers no questions, so the agent has to stop
safely instead of guessing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Protocol

from .policy import Action, Decision


@dataclass
class ApprovalRequest:
    rule_id: str
    reason: str
    action: Action
    intent: str = ""        # what the agent says it is trying to do


@dataclass
class ApprovalDecision:
    approved: bool
    note: str = ""
    by: str = "human"


class Human(Protocol):
    def ask(self, question: str, options: list[str] | None = None, reason: str = "") -> str: ...
    def approve(self, req: ApprovalRequest) -> ApprovalDecision: ...


C = {"b": "\033[1m", "y": "\033[33m", "c": "\033[36m", "g": "\033[32m", "r": "\033[31m", "d": "\033[2m", "x": "\033[0m"}


class ConsoleHuman:
    def __init__(self, stream=sys.stdout):
        self.out = stream

    def _p(self, s: str = "") -> None:
        print(s, file=self.out, flush=True)

    def ask(self, question: str, options: list[str] | None = None, reason: str = "") -> str:
        self._p()
        self._p(f"{C['c']}{C['b']}  ? The agent needs your input{C['x']}")
        if reason:
            self._p(f"{C['d']}    why: {reason}{C['x']}")
        self._p(f"    {question}")
        if options:
            for i, o in enumerate(options, 1):
                self._p(f"      {i}. {o}")
        ans = input(f"{C['c']}    your answer › {C['x']}").strip()
        if options and ans.isdigit() and 1 <= int(ans) <= len(options):
            ans = options[int(ans) - 1]
        return ans or "(no answer given)"

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        self._p()
        self._p(f"{C['y']}{C['b']}  ✋ Approval required{C['x']} {C['d']}[{req.rule_id}]{C['x']}")
        self._p(f"    {req.reason}")
        if req.intent:
            self._p(f"{C['d']}    agent's intent: {req.intent}{C['x']}")
        self._p("    The agent is about to:")
        for line in req.action.summary().splitlines():
            self._p(f"      {line}")
        while True:
            ans = input(f"{C['y']}    approve? [y]es / [n]o (optionally add a note, e.g. 'n wrong amount') › {C['x']}").strip()
            if not ans:
                continue
            head, _, note = ans.partition(" ")
            if head.lower() in ("y", "yes"):
                return ApprovalDecision(True, note.strip())
            if head.lower() in ("n", "no"):
                return ApprovalDecision(False, note.strip())


class ScriptedHuman:
    """Deterministic human for tests and evals."""

    def __init__(self, answers: list[str] | Callable[[str], str] | None = None,
                 approve: bool | Callable[[ApprovalRequest], bool] = True):
        self._answers = answers
        self._approve = approve
        self.questions: list[str] = []
        self.approvals: list[ApprovalRequest] = []

    def ask(self, question: str, options: list[str] | None = None, reason: str = "") -> str:
        self.questions.append(question)
        if callable(self._answers):
            return self._answers(question)
        if self._answers:
            return self._answers.pop(0)
        return "No further information is available. Use your best judgement within company policy."

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        self.approvals.append(req)
        ok = self._approve(req) if callable(self._approve) else self._approve
        return ApprovalDecision(ok, "scripted", by="script")


class UnattendedHuman(ScriptedHuman):
    def __init__(self):
        super().__init__(answers=lambda q: "Nobody is available to answer right now. Do not guess: "
                                           "skip anything that needs this answer and report it in your summary.",
                         approve=False)
