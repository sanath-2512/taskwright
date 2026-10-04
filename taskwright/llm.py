"""Provider-neutral LLM client with native tool calling.

Two wire formats cover almost every provider:
  * Anthropic Messages API (Claude)
  * OpenAI Chat Completions (OpenAI, Gemini's OpenAI-compatible endpoint, Groq,
    OpenRouter, Ollama, vLLM, ...)

Internally the runtime speaks one message format:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool", "results": [{"id", "name", "content", "is_error"}], "note": str?}

Uses only the standard library for HTTP so there is nothing extra to install.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .tools.base import ToolSpec


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    args_error: str | None = None
    extra: dict | None = None     # provider data that must be echoed back (e.g. Gemini thought signatures)


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: dict = field(default_factory=dict)
    stop_reason: str = ""


RETRYABLE = {408, 409, 429, 500, 502, 503, 504, 529}


class QuotaExhausted(LLMError):
    """The model's quota is used up for a long time (e.g. a free tier's daily limit)."""


class LLM:
    provider = "base"

    def __init__(self, model: str, api_key: str, base_url: str, max_tokens: int = 4096,
                 timeout: int = 180, on_retry: Callable[[str], None] | None = None,
                 fallbacks: list[str] | None = None, min_interval: float = 0.0):
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.on_retry = on_retry or (lambda msg: None)
        self.fallbacks = list(fallbacks or [])   # models to fail over to when this one is out of quota
        self.rotate = False                       # also hop between models on short per-minute limits
        self._cooldown: dict[str, float] = {}
        self.min_interval = min_interval          # adaptive pacing between calls (grows on 429s)
        self._floor_interval = min_interval
        self._last_call = 0.0
        self.usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        self.models_used = [model]

    # -- public ---------------------------------------------------------------

    def complete(self, system, messages: list[dict], tools: list[ToolSpec],
                 force_tool: str | None = None) -> LLMResponse:
        """One model call, failing over to the next configured model if this one is exhausted."""
        while True:
            try:
                return self._complete(system, messages, tools, force_tool)
            except LLMError as e:
                gone = isinstance(e, QuotaExhausted) or (" 404" in str(e) and "model" in str(e).lower())
                if not (gone and self.fallbacks):
                    raise
                old, self.model = self.model, self.fallbacks.pop(0)
                self.models_used.append(self.model)
                self.min_interval = self._floor_interval
                self.on_retry(f"model {old} is unavailable ({str(e)[:90]}); failing over to {self.model}")

    def _complete(self, system, messages, tools, force_tool) -> LLMResponse:
        raise NotImplementedError

    def structured(self, system, prompt: str, name: str, description: str, schema: dict) -> dict:
        """Get a JSON object matching `schema` by forcing a single tool call."""
        spec = ToolSpec(name, description, schema)
        messages: list[dict] = [{"role": "user", "content": prompt}]
        force: str | None = name
        for _ in range(3):
            try:
                resp = self.complete(system, messages, [spec], force_tool=force)
            except LLMError as e:
                if force and " 400" in str(e):   # provider does not support forcing a tool: just ask
                    force = None
                    continue
                raise
            call = next((c for c in resp.tool_calls if c.name == name and not c.args_error), None)
            if call:
                return call.args
            parsed = _extract_json(resp.text)
            if isinstance(parsed, dict):
                return parsed
            messages.append({"role": "assistant", "content": resp.text or "(no output)", "tool_calls": []})
            messages.append({"role": "user", "content": f"Reply only by calling the {name} tool with valid arguments."})
        raise LLMError(f"Model did not return valid {name} output.")

    # -- http -----------------------------------------------------------------

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        delay = 2.0
        last = ""
        for attempt in range(1, 9):
            body["model"] = self.model
            data = json.dumps(body).encode()
            gap = self._last_call + self.min_interval - time.time()
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.time()
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"content-type": "application/json", "user-agent": "taskwright/0.2",
                                                  **headers})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    self.usage["calls"] += 1
                    self.min_interval = max(self._floor_interval, self.min_interval * 0.85)
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:1500]
                if e.code not in RETRYABLE:
                    raise LLMError(f"{self.provider} API error {e.code}: {text[:800]}") from None
                hinted = _retry_after(e.headers.get("retry-after")) or _retry_hint(text)
                if e.code == 429 and hinted and hinted > 120:
                    raise QuotaExhausted(f"quota exhausted for {self.model} (resets in {hinted / 3600:.1f}h)")
                wait = min(hinted or delay, 65.0)
                last = f"HTTP {e.code}"
                if e.code == 429 and self.rotate and self.fallbacks:
                    # Per-minute limits are per model: use another model's budget instead of waiting.
                    self._cooldown[self.model] = time.time() + wait
                    pool = [m for m in self.fallbacks if self._cooldown.get(m, 0) <= time.time()]
                    if pool:
                        old = self.model
                        self.fallbacks.remove(pool[0])
                        self.fallbacks.append(old)
                        self.model = pool[0]
                        if self.model not in self.models_used:
                            self.models_used.append(self.model)
                        self.on_retry(f"{old} is rate limited; continuing on {self.model}")
                        continue
                    wait = max(0.0, min(self._cooldown.get(m, 0) for m in self.fallbacks) - time.time())
                elif e.code == 429:   # back off globally, not just for this call
                    self.min_interval = min(max(self.min_interval * 2, 4.0), 30.0)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                wait, last = delay, f"{type(e).__name__}: {e}"
            if attempt == 8:
                break
            self.on_retry(f"LLM call failed ({last}); retrying in {wait:.0f}s (attempt {attempt}/7)")
            time.sleep(wait)
            delay = min(delay * 2, 40)
        raise LLMError(f"{self.provider} API unavailable after retries ({last}).")


def _retry_after(v) -> float | None:
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _retry_hint(text: str) -> float | None:
    """Providers put the wait in the body: 'retry in 20h10m5.4s', '"retryDelay": "37s"', 'try again in 7.66s'."""
    m = re.search(r"(?:retry|try again) in ((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", text) or \
        re.search(r'"retryDelay":\s*"((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)"', text)
    if not m or not m.group(1):
        return None
    parts = {unit: float(n) for n, unit in re.findall(r"([\d.]+)([hms])", m.group(1))}
    return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0) + 1


def _extract_json(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------- Anthropic

class AnthropicLLM(LLM):
    provider = "anthropic"

    def _complete(self, system, messages, tools, force_tool=None) -> LLMResponse:
        if isinstance(system, (list, tuple)):
            # The static part (with the tool list before it) is cached; the live state block is not.
            sys_blocks = [{"type": "text", "text": system[0], "cache_control": {"type": "ephemeral"}}]
            sys_blocks += [{"type": "text", "text": x} for x in system[1:] if x]
        else:
            sys_blocks = system
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": sys_blocks,
                "messages": self._convert(messages)}
        if tools:
            body["tools"] = [t.to_dict() for t in tools]
            if force_tool:
                body["tool_choice"] = {"type": "tool", "name": force_tool}
        data = self._post(f"{self.base_url}/v1/messages",
                          {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}, body)
        text, calls = [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text.append(block["text"])
            elif block.get("type") == "tool_use":
                inp = block.get("input")
                calls.append(ToolCall(block["id"], block["name"], inp if isinstance(inp, dict) else {},
                                      None if isinstance(inp, dict) else "arguments were not an object"))
        u = data.get("usage", {})
        self.usage["input_tokens"] += u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) \
            + u.get("cache_creation_input_tokens", 0)
        self.usage["cached_tokens"] = self.usage.get("cached_tokens", 0) + u.get("cache_read_input_tokens", 0)
        self.usage["output_tokens"] += u.get("output_tokens", 0)
        return LLMResponse("\n".join(text).strip(), calls, u, data.get("stop_reason", ""))

    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:     # the API requires alternating roles
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": blocks})

        for m in messages:
            if m["role"] == "user":
                push("user", [{"type": "text", "text": m["content"]}])
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                           for c in m.get("tool_calls", [])]
                push("assistant", blocks or [{"type": "text", "text": "(continuing)"}])
            elif m["role"] == "tool":
                blocks = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["content"] or "(empty)",
                           "is_error": bool(r.get("is_error"))} for r in m["results"]]
                if m.get("note"):
                    blocks.append({"type": "text", "text": m["note"]})
                push("user", blocks)
        return out


# --------------------------------------------------------------------------- OpenAI-compatible

class OpenAICompatLLM(LLM):
    provider = "openai"

    @property
    def _is_gemini(self) -> bool:
        return "generativelanguage.googleapis.com" in self.base_url

    def _complete(self, system, messages, tools, force_tool=None) -> LLMResponse:
        if isinstance(system, (list, tuple)):
            system = "".join(system)
        body: dict = {"model": self.model, "messages": [{"role": "system", "content": system}] + self._convert(messages)}
        if tools:
            body["tools"] = []
            for t in tools:
                fn = {"name": t.name, "description": t.description}
                if t.input_schema.get("properties"):
                    params = dict(t.input_schema)
                    if not params.get("required"):
                        params.pop("required", None)   # some providers reject an empty list
                    fn["parameters"] = params
                body["tools"].append({"type": "function", "function": fn})
            if force_tool:
                # "required" with a single tool forces that tool and is the most widely supported form.
                body["tool_choice"] = "required" if len(tools) == 1 else {"type": "function", "function": {"name": force_tool}}
        data = self._post(f"{self.base_url}/chat/completions", {"authorization": f"Bearer {self.api_key}"}, body)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError):
            raise LLMError(f"Unexpected response: {json.dumps(data)[:500]}")
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                err = None if isinstance(args, dict) else "arguments were not an object"
            except json.JSONDecodeError as e:
                args, err = {}, f"invalid JSON arguments: {e}"
            calls.append(ToolCall(tc.get("id") or f"call_{int(time.time()*1000)}_{i}", fn.get("name", ""),
                                  args if isinstance(args, dict) else {}, err,
                                  {"extra_content": tc["extra_content"], "_model": self.model}
                                  if tc.get("extra_content") else None))
        u = data.get("usage") or {}
        self.usage["input_tokens"] += u.get("prompt_tokens", 0)
        self.usage["output_tokens"] += u.get("completion_tokens", 0)
        return LLMResponse((msg.get("content") or "").strip(), calls, u, data["choices"][0].get("finish_reason", ""))

    def _convert(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [self._tool_call_out(c) for c in m["tool_calls"]]
                elif not msg["content"]:
                    msg["content"] = "(continuing)"
                out.append(msg)
            elif m["role"] == "tool":
                for r in m["results"]:
                    content = r["content"] or "(empty)"
                    if r.get("is_error") and not content.startswith("ERROR"):
                        content = "ERROR: " + content
                    out.append({"role": "tool", "tool_call_id": r["id"], "content": content})
                if m.get("note"):
                    out.append({"role": "user", "content": m["note"]})
        return out

    def _tool_call_out(self, c: dict) -> dict:
        tc = {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
        if self._is_gemini and self.model.startswith("gemini"):
            # Gemini 3 requires each earlier function call to carry the thought signature it was issued with.
            # A call made by another model (after a failover) gets the documented "skip validation" value.
            extra = c.get("extra") or {}
            if extra.get("_model") == self.model and extra.get("extra_content"):
                tc["extra_content"] = extra["extra_content"]
            else:
                tc["extra_content"] = {"google": {"thought_signature": "skip_thought_signature_validator"}}
        return tc


# --------------------------------------------------------------------------- factory

PROVIDERS = {
    "anthropic": {"cls": AnthropicLLM, "key_env": "ANTHROPIC_API_KEY", "base": "https://api.anthropic.com",
                  "model": "claude-sonnet-5-5"},
    "openai": {"cls": OpenAICompatLLM, "key_env": "OPENAI_API_KEY", "base": "https://api.openai.com/v1",
               "model": "gpt-4.1"},
    # Free-tier Gemini keys get a small daily quota per model, so the default chain fails over across models.
    "gemini": {"cls": OpenAICompatLLM, "key_env": "GEMINI_API_KEY",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-3.8-flash",
               "fallbacks": ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview",
                             "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview"]},
    "openrouter": {"cls": OpenAICompatLLM, "key_env": "OPENROUTER_API_KEY", "base": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-4.5"},
    # Free-tier Groq allows few tokens per minute per model, so calls rotate across models with separate budgets.
    "groq": {"cls": OpenAICompatLLM, "key_env": "GROQ_API_KEY", "base": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "fallbacks": ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"], "rotate": True},
    "ollama": {"cls": OpenAICompatLLM, "key_env": "OLLAMA_API_KEY", "base": "http://localhost:11434/v1",
               "model": "qwen2.5:14b"},
}


def detect_provider() -> str | None:
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for name in ("anthropic", "openai", "gemini", "openrouter", "groq"):
        if os.environ.get(PROVIDERS[name]["key_env"]):
            return name
    return None


def make_llm(provider: str | None = None, model: str | None = None, on_retry=None) -> LLM:
    provider = (provider or detect_provider() or "").lower()
    if provider not in PROVIDERS:
        raise LLMError("No LLM configured. Set one of ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, "
                       "OPENROUTER_API_KEY or GROQ_API_KEY in .env (see .env.example), or LLM_PROVIDER=ollama.")
    cfg = PROVIDERS[provider]
    key = os.environ.get(cfg["key_env"], "ollama" if provider == "ollama" else "")
    if not key:
        raise LLMError(f"{cfg['key_env']} is not set (needed for provider '{provider}').")
    chosen = model or os.environ.get("LLM_MODEL") or cfg["model"]
    env_fb = os.environ.get("LLM_FALLBACK_MODELS")
    fallbacks = [m.strip() for m in env_fb.split(",") if m.strip()] if env_fb is not None else cfg.get("fallbacks", [])
    llm = cfg["cls"](model=chosen, api_key=key, base_url=os.environ.get("LLM_BASE_URL") or cfg["base"],
                     on_retry=on_retry, fallbacks=[m for m in fallbacks if m != chosen],
                     min_interval=float(os.environ.get("LLM_MIN_INTERVAL", "0")))
    llm.provider = provider
    llm.rotate = bool(cfg.get("rotate")) and os.environ.get("LLM_ROTATE", "1") != "0"
    return llm
