"""A deterministic stand-in for the LLM, used only by tests.

It reacts to what the tools actually return (page snapshots, errors), so the
real browser, sandbox, policy engine, approval flow, retry logic, verifier and
report are all exercised end to end without network access or API costs.
It is not used for demos: the demo always runs a real model.
"""

from __future__ import annotations

import re

from taskwright.llm import LLM, LLMResponse, ToolCall


def ref(text: str, kind: str, label: str) -> str | None:
    m = re.search(r"\[(e\d+) " + kind + r' "' + re.escape(label), text or "")
    return m.group(1) if m else None


class ScriptedLLM(LLM):
    provider = "scripted"

    def __init__(self, brain):
        super().__init__(model="scripted", api_key="", base_url="")
        self.brain = brain
        self.n = 0

    def _complete(self, system, messages, tools, force_tool=None) -> LLMResponse:
        self.usage["calls"] += 1
        self.n += 1
        names = [t.name for t in tools]
        last = ""
        if messages and messages[-1]["role"] == "tool":
            last = "\n".join(r["content"] for r in messages[-1]["results"])
        elif messages:
            last = messages[-1].get("content", "")
        text, calls = self.brain(force_tool, names, last, messages)
        return LLMResponse(text, [ToolCall(f"c{self.n}_{i}", n, a) for i, (n, a) in enumerate(calls)])


def call(name, **args):
    return (name, args)


class InvoiceBrain:
    """Behaves like a careful clerk doing demo task 1, including making (and fixing) a date-format mistake."""

    def __init__(self):
        self.stage = "start"
        self.verifier_stage = "start"
        self.form_attempts = 0

    def __call__(self, force_tool, names, last, messages):
        if force_tool == "submit_brief":
            return "", [call("submit_brief", goal="Record Acme's latest invoice in Ledgerly.",
                             interpretation="Latest = revised INV-2026-0912-R1 (AP-4).",
                             success_criteria=["Ledgerly has exactly one record for Acme invoice INV-2026-0912-R1 "
                                               "with amount 52280.00 and due date 30/10/2026",
                                               "The superseded INV-2026-0912 is not recorded"],
                             plan=["Find Acme's latest invoice email", "Read the PDF", "Record it in Ledgerly",
                                   "Confirm the record"],
                             relevant_procedures=["AP-2", "AP-3", "AP-4"], risks=["revised invoice"],
                             blocking_questions=[])]
        if force_tool == "submit_lessons":
            return "", [call("submit_lessons", lessons=[{"lesson": "Ledgerly dates must be typed as DD/MM/YYYY.",
                                                         "applies_to": ["ledgerly", "dates"], "kind": "system_quirk"}])]
        if "report_verdict" in names:
            return self.verify(last)
        return self.work(last)

    def work(self, last):
        s = self.stage
        if s == "start":
            self.stage = "inbox"
            return "Search the mailbox for Acme.", [call("browser_open", url="/mail?q=acme")]
        if s == "inbox":
            self.stage = "email"
            return "The newest Acme email is the revised copy.", [
                call("browser_click", ref=ref(last, "link", "Re: Invoice INV-2026-0912"))]
        if s == "email":
            self.stage = "download"
            return "", [call("browser_click", ref=ref(last, "link", "INV-2026-0912-R1.pdf"))]
        if s == "download":
            self.stage = "read"
            return "", [call("read_file", path="downloads/INV-2026-0912-R1.pdf")]
        if s == "read":
            self.stage = "erp"
            return "", [call("remember", key="amount", value="52280.00", source="INV-2026-0912-R1.pdf Total Payable"),
                        call("remember", key="due_date", value="30 Oct 2026", source="INV-2026-0912-R1.pdf Due Date"),
                        call("browser_open", url="/erp/invoices/new")]
        if "Sign in to Ledgerly" in last:
            return "Need to sign in.", [
                call("browser_fill_form", fields=[{"ref": ref(last, "textbox", "Username"), "value": "{{secret:LEDGERLY_USERNAME}}"},
                                                  {"ref": ref(last, "password", "Password"), "value": "{{secret:LEDGERLY_PASSWORD}}"}]),
                call("browser_click", ref=ref(last, "button", "Sign in"))]
        if "Record a vendor invoice" in last and "[e" in last:
            self.form_attempts += 1
            date_fmt = "2026-10-30" if self.form_attempts <= 2 else "30/10/2026"
            inv_date = "2026-09-30" if self.form_attempts <= 2 else "30/09/2026"
            return f"Fill the form (attempt {self.form_attempts}).", [
                call("browser_fill_form", fields=[
                    {"ref": ref(last, "select", "Vendor"), "value": "Acme Industrial"},
                    {"ref": ref(last, "textbox", "Invoice number"), "value": "INV-2026-0912-R1"},
                    {"ref": ref(last, "textbox", "PO number"), "value": "PO-4471"},
                    {"ref": ref(last, "textbox", "Invoice date"), "value": inv_date},
                    {"ref": ref(last, "textbox", "Due date"), "value": date_fmt},
                    {"ref": ref(last, "textbox", "Amount (INR)"), "value": "52280.00"}]),
                call("browser_click", ref=ref(last, "button", "Save invoice"))]
        if "Invoice recorded as" in last:
            rid = re.search(r"Invoice recorded as (AP-\d+)", last).group(1)
            return "Done.", [call("browser_screenshot", caption="Ledgerly record created"),
                             call("finish", status="completed", summary=f"Recorded as {rid}.",
                                  claims=[f"Ledgerly record {rid} exists for INV-2026-0912-R1, amount 52280.00, "
                                          "due 30/10/2026"])]
        if "Typed into" in last or "Selected" in last:
            return "", [call("browser_snapshot")]
        return "Look at the page.", [call("browser_snapshot")]

    def verify(self, last):
        if self.verifier_stage == "start":
            self.verifier_stage = "look"
            return "", [call("browser_open", url="/erp/invoices?q=INV-2026-0912")]
        if "Sign in to Ledgerly" in last:
            return "", [call("browser_fill_form", fields=[
                {"ref": ref(last, "textbox", "Username"), "value": "{{secret:LEDGERLY_USERNAME}}"},
                {"ref": ref(last, "password", "Password"), "value": "{{secret:LEDGERLY_PASSWORD}}"}]),
                call("browser_click", ref=ref(last, "button", "Sign in"))]
        if "Invoices (" in last:
            ok = "INV-2026-0912-R1" in last and "52,280.00" in last and "30/10/2026" in last
            dup = re.search(r"\| INV-2026-0912 \|", last) is not None
            return "", [call("browser_screenshot", caption="Ledgerly list"),
                        call("report_verdict", criteria=[
                            {"criterion": "record exists", "verdict": "pass" if ok else "fail", "evidence": "list"},
                            {"criterion": "original not recorded", "verdict": "fail" if dup else "pass", "evidence": "list"}],
                            side_effects=[], overall="verified" if ok and not dup else "failed", summary="checked")]
        return "", [call("browser_snapshot")]
