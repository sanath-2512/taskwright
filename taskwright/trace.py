"""Append-only event log for a run. Everything the console, the report and the
lessons reflection know about a run comes from these events."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable


class Trace:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.events: list[dict] = []
        self.listeners: list[Callable[[dict], None]] = []
        self.t0 = time.time()

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self.listeners.append(fn)

    def emit(self, type: str, **data) -> dict:
        ev = {"type": type, "t": round(time.time() - self.t0, 2), **data}
        self.events.append(ev)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:  # a broken listener must never break a run
                pass
        return ev

    def of_type(self, *types: str) -> list[dict]:
        return [e for e in self.events if e["type"] in types]
