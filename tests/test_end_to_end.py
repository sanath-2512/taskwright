"""End-to-end test of the runtime with a scripted model against the real sandbox in a real browser.

Covers: understand -> execute -> injected 503 auto-retry -> approval -> session expiry mid-submit ->
re-login -> validation error -> correction -> approval reuse -> finish -> independent verification ->
lessons -> report.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

STATE = tempfile.mkdtemp(prefix="tw_sandbox_")
os.environ["SANDBOX_STATE_DIR"] = STATE

from taskwright.cli import ensure_sandbox  # noqa: E402
from taskwright.config import Settings  # noqa: E402
from taskwright.human import ScriptedHuman  # noqa: E402
from taskwright.runtime import Run  # noqa: E402

from tests.scripted_llm import InvoiceBrain, ScriptedLLM  # noqa: E402

BASE = "http://127.0.0.1:8799"


@pytest.fixture(scope="module")
def sandbox():
    ensure_sandbox(BASE, quiet=True)
    from sandbox.app import store
    store.reset(chaos=True)
    yield store


def test_invoice_flow_end_to_end(sandbox, tmp_path):
    s = Settings()
    s.base_url = BASE
    s.runs_dir = tmp_path / "runs"
    s.memory_dir = tmp_path / "memory"
    human = ScriptedHuman(approve=True)
    events = []
    run = Run(s, "Find the latest invoice from Acme and enter it into our AP system.", ScriptedLLM(InvoiceBrain()),
              human, listeners=[events.append])
    result = run.execute()

    assert result["status"] == "completed", result
    assert result["verification"] == "verified", result
    ledger = sandbox.read()["ledger"]
    recs = [r for r in ledger if r["invoice_number"] == "INV-2026-0912-R1"]
    assert len(recs) == 1 and recs[0]["amount"] == 52280.0 and recs[0]["due_date"] == "30/10/2026"

    types = [e["type"] for e in events]
    assert "retry" in types                         # the injected 503 was retried automatically
    assert len(human.approvals) == 1                # high-value invoice needed approval exactly once...
    assert "approval_reused" in types               # ...and the identical resubmission reused it
    warnings = [e["warning"] for e in events if e["type"] == "tool_result" and e.get("warning")]
    assert any("session has expired" in w for w in warnings)      # chaos: session expiry mid-submit
    assert any("DD/MM/YYYY" in w for w in warnings)               # validation error surfaced
    assert Path(result["report"]).exists()
    assert (Path(result["run_dir"]) / "trace.jsonl").exists()
    lessons = (s.memory_dir / "lessons.jsonl").read_text()
    assert "DD/MM/YYYY" in lessons
    trace = (Path(result["run_dir"]) / "trace.jsonl").read_text()
    assert "Northstar#2026" not in trace            # the password never reaches logs
    assert "Northstar#2026" not in Path(result["report"]).read_text()
