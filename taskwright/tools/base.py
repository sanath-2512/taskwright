"""Tool plumbing shared by every toolkit.

A tool is a plain method decorated with @tool. The decorator records a JSON
schema that is handed to the LLM; the runtime dispatches calls by name.
Every tool returns a ToolResult. Failures are *returned*, not raised, with an
error_kind the runtime uses to decide between retrying automatically and
handing the problem back to the agent to reason about.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

# error kinds
TRANSIENT = "transient"   # timeouts, 5xx: safe to retry automatically if the tool is idempotent
STALE = "stale"           # element ref no longer exists: take a new snapshot
INVALID = "invalid"       # bad arguments
BLOCKED = "blocked"       # policy forbids the action
DENIED = "denied"         # a human declined the approval
NOT_FOUND = "not_found"
FAILED = "failed"         # anything else


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    error_kind: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # A shorter version used when the result is old and the context needs compacting.
    compact: str | None = None

    @classmethod
    def error(cls, kind: str, message: str, **data) -> "ToolResult":
        return cls(content=f"ERROR ({kind}): {message}", is_error=True, error_kind=kind, data=data)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    idempotent: bool = True       # safe for the runtime to retry on transient failure
    fn: Callable[..., ToolResult] | None = None

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


def tool(name: str, description: str, properties: dict | None = None,
         required: list[str] | None = None, idempotent: bool = True):
    """Mark a method as an LLM-callable tool."""
    schema = {"type": "object", "properties": properties or {}, "required": required or []}

    def deco(fn):
        fn._tool_spec = ToolSpec(name=name, description=description.strip(), input_schema=schema,
                                 idempotent=idempotent)
        return fn
    return deco


def collect_tools(*toolkits, exclude: set[str] | None = None) -> dict[str, ToolSpec]:
    """Build a name -> bound ToolSpec registry from toolkit instances."""
    registry: dict[str, ToolSpec] = {}
    for kit in toolkits:
        for _, method in inspect.getmembers(kit, predicate=inspect.ismethod):
            spec = getattr(method, "_tool_spec", None)
            if spec and not (exclude and spec.name in exclude):
                registry[spec.name] = ToolSpec(spec.name, spec.description, spec.input_schema,
                                               spec.idempotent, method)
    return registry
