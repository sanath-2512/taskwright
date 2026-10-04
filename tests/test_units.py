from pathlib import Path

import pytest

from taskwright.config import ROOT
from taskwright.llm import AnthropicLLM, OpenAICompatLLM
from taskwright.memory import KnowledgeBase, LessonStore
from taskwright.policy import COMMIT, LOGIN, Action, PolicyEngine
from taskwright.vault import SecretError, Vault

POLICIES = ROOT / "config" / "policies.toml"


def invoice(amount):
    return Action(COMMIT, "http://127.0.0.1:8765/erp/invoices/new", "Save invoice",
                  {"vendor": "V-1001", "invoice_number": "X-1", "amount": amount, "due_date": "30/10/2026"},
                  target="http://127.0.0.1:8765/erp/invoices/new")


def email(to):
    return Action(COMMIT, "http://127.0.0.1:8765/mail/compose", "Send", {"to": to, "subject": "Hi", "body": "x"},
                  target="http://127.0.0.1:8765/mail/compose")


def test_policy_thresholds_and_scopes():
    p = PolicyEngine.load(POLICIES, autonomy="standard")
    assert p.evaluate(invoice("52280.00")).effect == "approve"
    assert p.evaluate(invoice("52,280.00")).effect == "approve"        # formatting does not dodge the rule
    assert p.evaluate(invoice("17700.00")).effect == "allow"
    assert p.evaluate(email("finance@umbrella-fs.example")).effect == "approve"
    assert p.evaluate(email("security@northstar.example")).effect == "allow"
    assert p.evaluate(Action(COMMIT, "http://x/erp/invoices/AP-1", "Delete invoice")).effect == "deny"
    assert p.evaluate(Action(LOGIN, "http://x/erp/login", "Sign in")).effect == "allow"


def test_autonomy_levels_and_read_only():
    assert PolicyEngine.load(POLICIES, autonomy="supervised").evaluate(invoice("10")).effect == "approve"
    assert PolicyEngine.load(POLICIES, autonomy="autonomous").evaluate(invoice("99999")).effect == "allow"
    assert PolicyEngine.load(POLICIES, autonomy="autonomous").evaluate(
        Action(COMMIT, "http://x", "Delete")).effect == "deny"                 # deny rules always apply
    assert PolicyEngine([], read_only=True).evaluate(invoice("1")).effect == "deny"


def test_approval_is_bound_to_meaning_not_formatting():
    p = PolicyEngine.load(POLICIES)
    a = invoice("52,280.00")
    p.record_approval("ap-high-value-invoice", a)
    same = invoice("52280")
    same.fields["due_date"] = "2026-10-30"
    assert p.already_approved("ap-high-value-invoice", same)
    assert not p.already_approved("ap-high-value-invoice", invoice("52290.00"))


def test_vault_scopes_and_redacts():
    v = Vault.load(ROOT / "config" / "vault.toml")
    assert v.resolve("{{secret:LEDGERLY_PASSWORD}}", "http://127.0.0.1:8765/erp/login") == "Northstar#2026"
    with pytest.raises(SecretError):
        v.resolve("{{secret:LEDGERLY_PASSWORD}}", "https://evil.example/login")
    with pytest.raises(SecretError):
        v.resolve("{{secret:NOPE}}", "http://127.0.0.1/")
    assert "Northstar#2026" not in v.redact("password is Northstar#2026")


def test_knowledge_retrieval(tmp_path):
    kb = KnowledgeBase(ROOT / "company" / "handbook", "http://127.0.0.1:8765", LessonStore(tmp_path / "l.jsonl"))
    top = kb.search("Acme registered billing email", k=2)
    assert top and top[0][0].id.startswith("03-vendor-directory")
    top = kb.search("approval threshold for high value invoices", k=2)
    assert any("AP-3" in d.id for d, _ in top)


def test_lessons_dedupe(tmp_path):
    store = LessonStore(tmp_path / "lessons.jsonl")
    store.add([{"lesson": "Ledgerly dates must be typed as DD/MM/YYYY.", "applies_to": ["ledgerly"]}], "r1")
    store.add([{"lesson": "Ledgerly dates must be typed as DD/MM/YYYY", "applies_to": ["ledgerly"]}], "r2")
    assert len(store.all()) == 1
    assert store.relevant("record an invoice in ledgerly")


def test_message_conversion():
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "t", "args": {"a": 1}}]},
            {"role": "tool", "results": [{"id": "1", "name": "t", "content": "ok", "is_error": False}], "note": "nudge"},
            {"role": "user", "content": "more"}]
    a = AnthropicLLM._convert(msgs)
    assert [m["role"] for m in a] == ["user", "assistant", "user"]       # tool results + note + user merged
    assert a[2]["content"][0]["type"] == "tool_result" and a[2]["content"][-1]["text"] == "more"
    o = OpenAICompatLLM("m", "k", "https://api.openai.com/v1")._convert(msgs)
    assert [m["role"] for m in o] == ["user", "assistant", "tool", "user", "user"]
    assert o[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


def _all_specs():
    from taskwright.tools.agent_tools import KnowledgeToolkit, VerdictToolkit, WorkerToolkit
    from taskwright.tools.browser import BrowserToolkit
    from taskwright.tools.files import FileToolkit
    specs = []
    for cls in (BrowserToolkit, FileToolkit, KnowledgeToolkit, WorkerToolkit, VerdictToolkit):
        specs += [v._tool_spec for v in vars(cls).values() if hasattr(v, "_tool_spec")]
    return specs


def test_tool_schemas_are_provider_safe():
    import re
    specs = _all_specs()
    assert len({s.name for s in specs}) == len(specs) >= 17
    for s in specs:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", s.name)
        assert s.input_schema["type"] == "object" and isinstance(s.input_schema["properties"], dict)
        assert set(s.input_schema["required"]) <= set(s.input_schema["properties"])
        assert s.description


def test_request_bodies(monkeypatch):
    from taskwright.tools.base import ToolSpec
    captured = {}
    tools = [ToolSpec("noop", "does nothing", {"type": "object", "properties": {}, "required": []}),
             ToolSpec("t", "t", {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]})]
    msgs = [{"role": "user", "content": "hi"}]

    a = AnthropicLLM("m", "k", "https://x")
    monkeypatch.setattr(a, "_post", lambda url, h, body: captured.update(url=url, h=h, body=body) or
                        {"content": [{"type": "tool_use", "id": "1", "name": "t", "input": {"a": "x"}}],
                         "usage": {"input_tokens": 5, "output_tokens": 1}})
    r = a.complete(("static", "dynamic"), msgs, tools)
    assert captured["url"].endswith("/v1/messages") and captured["h"]["anthropic-version"]
    assert captured["body"]["system"][0]["cache_control"] and captured["body"]["system"][1]["text"] == "dynamic"
    assert r.tool_calls[0].args == {"a": "x"}

    o = OpenAICompatLLM("m", "k", "https://y/v1")
    monkeypatch.setattr(o, "_post", lambda url, h, body: captured.update(url=url, body=body) or
                        {"choices": [{"message": {"content": None, "tool_calls": [
                            {"id": "c", "function": {"name": "t", "arguments": '{"a": "y"}'}}]}, "finish_reason": "tool_calls"}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 1}})
    r = o.complete(("static", "dynamic"), msgs, tools, force_tool="t")
    body = captured["body"]
    assert captured["url"] == "https://y/v1/chat/completions"
    assert body["messages"][0] == {"role": "system", "content": "staticdynamic"}
    assert "parameters" not in body["tools"][0]["function"]                 # no-arg tool: no empty schema
    assert body["tools"][1]["function"]["parameters"]["required"] == ["a"]
    assert r.tool_calls[0].args == {"a": "y"}


def test_retry_hints_and_gemini_signatures():
    from taskwright.llm import _retry_hint
    assert _retry_hint("Please retry in 20h10m5.4s.") > 20 * 3600
    assert 37 <= _retry_hint('"retryDelay": "37s"') <= 39
    assert 7 < _retry_hint("Please try again in 7.66s.") < 9
    g = OpenAICompatLLM("gemini-3.5-flash", "k", "https://generativelanguage.googleapis.com/v1beta/openai")
    own = {"id": "1", "name": "t", "args": {}, "extra": {"extra_content": {"google": {"thought_signature": "abc"}},
                                                            "_model": "gemini-3.5-flash"}}
    foreign = {"id": "2", "name": "t", "args": {}, "extra": {"extra_content": {"google": {"thought_signature": "x"}},
                                                                "_model": "gemini-3.8-flash"}}
    assert g._tool_call_out(own)["extra_content"]["google"]["thought_signature"] == "abc"
    assert g._tool_call_out(foreign)["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"
    o = OpenAICompatLLM("gpt", "k", "https://api.openai.com/v1")
    assert "extra_content" not in o._tool_call_out(own)        # never leak provider fields to other APIs


def test_failover_on_quota(monkeypatch):
    from taskwright.llm import LLMResponse, QuotaExhausted
    llm = OpenAICompatLLM("a", "k", "https://x", fallbacks=["b"])
    seen = []

    def fake(system, messages, tools, force_tool):
        seen.append(llm.model)
        if llm.model == "a":
            raise QuotaExhausted("daily limit")
        return LLMResponse("ok", [])
    monkeypatch.setattr(llm, "_complete", fake)
    assert llm.complete("s", [], []).text == "ok" and seen == ["a", "b"]
