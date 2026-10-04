"""Mission control: a small web UI for giving the worker tasks, watching it live, and answering
approvals and questions.   python -m taskwright ui   ->   http://127.0.0.1:8800

The worker runs in a background thread. Its trace events stream to the page over Server-Sent
Events, and the page answers approvals/questions through WebHuman, which blocks the worker until a
person responds. Same runtime, same tools as the CLI; only the human channel differs.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .human import ApprovalDecision, ApprovalRequest
from .memory import LessonStore

UI_HTML = (Path(__file__).parent / "web_ui.html").read_text(encoding="utf-8")


class WebHuman:
    """Blocks the worker thread until the browser answers."""

    def __init__(self):
        self.emit: Callable[..., dict] = lambda *a, **k: {}
        self._pending: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _wait(self, kind: str, payload: dict) -> dict:
        rid = f"h{next(self._ids)}"
        ev = threading.Event()
        with self._lock:
            self._pending[rid] = {"event": ev, "response": None}
        self.emit("awaiting_human", id=rid, kind=kind, **payload)
        ev.wait()
        with self._lock:
            resp = self._pending.pop(rid)["response"]
        self.emit("human_responded", id=rid, kind=kind)
        return resp

    def respond(self, rid: str, response: dict) -> bool:
        with self._lock:
            item = self._pending.get(rid)
            if not item:
                return False
            item["response"] = response
            item["event"].set()
            return True

    def cancel_all(self) -> None:
        with self._lock:
            for item in self._pending.values():
                item["response"] = {"cancelled": True}
                item["event"].set()

    def ask(self, question: str, options: list[str] | None = None, reason: str = "") -> str:
        r = self._wait("question", {"question": question, "options": options or [], "why": reason})
        return r.get("answer") or "(no answer given)"

    def approve(self, req: ApprovalRequest) -> ApprovalDecision:
        a = req.action
        fields = [{"label": a.field_labels.get(k) or k, "value": v} for k, v in a.fields.items() if v]
        r = self._wait("approval", {"rule": req.rule_id, "reason": req.reason, "intent": req.intent,
                                    "action": {"kind": a.kind, "label": a.label, "url": a.url, "fields": fields}})
        return ApprovalDecision(bool(r.get("approved")), r.get("note", ""), by="web")


class RunManager:
    def __init__(self, settings: Settings, llm_factory: Callable):
        self.s = settings
        self.llm_factory = llm_factory
        self.runs: dict[str, dict] = {}
        self.active: str | None = None
        self._lock = threading.Lock()

    def start(self, task: str, autonomy: str | None, headed: bool) -> str:
        from .runtime import Run
        with self._lock:
            if self.active and self.runs[self.active]["thread"].is_alive():
                raise HTTPException(409, "A task is already running.")
            s = Settings()
            s.base_url, s.runs_dir, s.memory_dir = self.s.base_url, self.s.runs_dir, self.s.memory_dir
            s.autonomy = autonomy or None
            s.headed = headed
            s.slow_mo = 150 if headed else 0
            human = WebHuman()
            events: list[dict] = []
            from .console import ConsoleView
            run = Run(s, task, self.llm_factory(), human, listeners=[events.append, ConsoleView()])
            human.emit = run.trace.emit
            rec = {"run": run, "human": human, "events": events, "result": None}
            t = threading.Thread(target=self._go, args=(rec,), daemon=True)
            rec["thread"] = t
            self.runs[run.run_id] = rec
            self.active = run.run_id
            t.start()
            return run.run_id

    @staticmethod
    def _go(rec: dict) -> None:
        try:
            rec["result"] = rec["run"].execute()
        except Exception as e:  # surface crashes to the UI instead of dying silently
            rec["run"].trace.emit("error", text=f"{type(e).__name__}: {e}")
            rec["run"].trace.emit("run_end", status="error", verification="not run", summary=str(e),
                                  follow_ups=[], steps=0, duration_s=0, usage={}, report="")


def create_app(settings: Settings | None = None, llm_factory: Callable | None = None) -> FastAPI:
    from .demo_tasks import TASKS
    from .llm import make_llm

    s = settings or Settings()
    s.runs_dir.mkdir(parents=True, exist_ok=True)
    mgr = RunManager(s, llm_factory or (lambda: make_llm(s.provider, s.model)))
    app = FastAPI(title="Taskwright", docs_url=None, redoc_url=None)
    app.mount("/files", StaticFiles(directory=str(s.runs_dir)), name="files")

    def to_url(path: str | None) -> str | None:
        if not path:
            return None
        try:
            return "/files/" + Path(path).resolve().relative_to(s.runs_dir.resolve()).as_posix()
        except ValueError:
            return None

    @app.get("/", response_class=HTMLResponse)
    def index():
        return UI_HTML

    @app.get("/api/info")
    def info():
        try:
            llm = make_llm(s.provider, s.model) if llm_factory is None else llm_factory()
            model = f"{llm.provider} / {llm.model}"
        except Exception as e:
            model = f"not configured: {e}"
        return {"tasks": TASKS, "model": model, "sandbox": s.base_url,
                "lessons": LessonStore(s.memory_dir / "lessons.jsonl").all(), "active": mgr.active}

    @app.post("/api/runs")
    async def start(request: Request):
        body = await request.json()
        task = (body.get("task") or "").strip()
        if not task:
            raise HTTPException(400, "Task is empty.")
        rid = await asyncio.to_thread(mgr.start, task, body.get("autonomy"), bool(body.get("headed")))
        return {"run_id": rid}

    @app.get("/api/runs/{rid}/events")
    async def events(rid: str):
        rec = mgr.runs.get(rid)
        if not rec:
            raise HTTPException(404)

        async def stream():
            i = 0
            while True:
                evs = rec["events"]
                while i < len(evs):
                    ev = dict(evs[i])
                    i += 1
                    for key in ("screenshot", "report"):
                        if ev.get(key):
                            ev[key + "_url"] = to_url(ev[key])
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                    if ev["type"] == "run_end":
                        return
                await asyncio.sleep(0.25)
                yield ": keep-alive\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{rid}/respond")
    async def respond(rid: str, request: Request):
        rec = mgr.runs.get(rid)
        if not rec:
            raise HTTPException(404)
        body = await request.json()
        if not rec["human"].respond(body.get("id", ""), body):
            raise HTTPException(409, "Nothing is waiting for that response.")
        return {"ok": True}

    @app.post("/api/reset")
    async def reset(request: Request):
        body = await request.json()
        if mgr.active and mgr.runs[mgr.active]["thread"].is_alive():
            raise HTTPException(409, "Wait for the running task to finish.")
        import urllib.request
        req = urllib.request.Request(f"{s.base_url.rstrip('/')}/__sandbox/reset?chaos={0 if body.get('no_chaos') else 1}",
                                     method="POST")
        await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=20).read())
        if body.get("memory"):
            LessonStore(s.memory_dir / "lessons.jsonl").clear()
        return {"ok": True}

    return app
