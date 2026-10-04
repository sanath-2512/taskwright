"""Command line entry point:  python -m taskwright <command>

  run "<task>"     run the worker on a natural-language task
  demo <id|n>      run one of the demo tasks
  tasks            list demo tasks
  eval             run demo tasks unattended and score them against ground truth
  sandbox          start the simulated company (mail, Ledgerly, handbook) in the foreground
  reset            reset the sandbox's data (add --memory to also forget learned lessons)
  memory           show what the worker has learned
  ui               mission control in the browser: give tasks, watch live, approve
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

from .config import Settings
from .console import ConsoleView, c


def _healthy(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/__sandbox/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_sandbox(base_url: str, quiet: bool = False) -> None:
    """Start the sandbox in a background thread if it is not already running locally."""
    if _healthy(base_url):
        return
    u = urlparse(base_url)
    if u.hostname not in ("127.0.0.1", "localhost"):
        sys.exit(f"Cannot reach {base_url}. Start the environment first.")
    import uvicorn
    from sandbox.app import app
    server = uvicorn.Server(uvicorn.Config(app, host=u.hostname, port=u.port or 80, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if _healthy(base_url):
            if not quiet:
                print(c(f"  (started the sandbox company at {base_url})", "gr"))
            return
        time.sleep(0.1)
    sys.exit("The sandbox did not start.")


def _post(url: str) -> dict:
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def cmd_run(args, task: str, answers: list[str] | None = None, scripted: bool = False) -> dict:
    from .human import ConsoleHuman, ScriptedHuman, UnattendedHuman
    from .llm import LLMError, make_llm
    from .runtime import Run

    s = Settings()
    if args.base_url:
        s.base_url = args.base_url
    if args.autonomy:
        s.autonomy = args.autonomy
    if args.max_steps:
        s.max_steps = args.max_steps
    if args.headed:
        s.headed = True
    if args.slow_mo is not None:
        s.slow_mo = args.slow_mo
    if getattr(args, "no_learn", False):
        s.learn = False
    ensure_sandbox(s.base_url)
    try:
        llm = make_llm(args.provider or s.provider, args.model or s.model)
    except LLMError as e:
        sys.exit(str(e))
    if scripted:
        human = ScriptedHuman(answers=list(answers or []), approve=True)
    elif args.unattended:
        human = UnattendedHuman()
    else:
        human = ConsoleHuman()
    run = Run(s, task, llm, human, listeners=[ConsoleView(verbose=args.verbose)])
    return run.execute()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="taskwright", description="Autonomous AI task worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    def run_opts(sp):
        sp.add_argument("--provider", help="anthropic | openai | gemini | openrouter | groq | ollama")
        sp.add_argument("--model")
        sp.add_argument("--autonomy", choices=["supervised", "standard", "autonomous"])
        sp.add_argument("--max-steps", type=int)
        sp.add_argument("--headed", action="store_true", help="show the browser window")
        sp.add_argument("--slow-mo", type=int, help="ms delay between browser actions (good for recording)")
        sp.add_argument("--unattended", action="store_true", help="nobody is watching: deny approvals, no answers")
        sp.add_argument("--no-learn", action="store_true", help="do not write lessons to long-term memory")
        sp.add_argument("--base-url")
        sp.add_argument("-v", "--verbose", action="store_true")

    sp = sub.add_parser("run", help="run a task")
    sp.add_argument("task", nargs="+")
    run_opts(sp)
    sp = sub.add_parser("demo", help="run a demo task by id or number")
    sp.add_argument("which")
    run_opts(sp)
    sub.add_parser("tasks", help="list demo tasks")
    sp = sub.add_parser("eval", help="run demo tasks unattended and score against ground truth")
    sp.add_argument("--only", nargs="*")
    run_opts(sp)
    sp = sub.add_parser("sandbox", help="run the simulated company in the foreground")
    sp.add_argument("--port", type=int, default=8765)
    sp = sub.add_parser("reset", help="reset sandbox data")
    sp.add_argument("--memory", action="store_true", help="also clear learned lessons")
    sp.add_argument("--no-chaos", action="store_true", help="disable injected failures")
    sp.add_argument("--base-url")
    sp = sub.add_parser("ui", help="mission control web UI")
    sp.add_argument("--port", type=int, default=8800)
    sp.add_argument("--provider")
    sp.add_argument("--model")
    sp = sub.add_parser("report", help="rebuild a run's report.html from its trace")
    sp.add_argument("run_dir")
    sp = sub.add_parser("memory", help="show learned lessons")
    sp.add_argument("--clear", action="store_true")

    args = p.parse_args(argv)
    from .demo_tasks import TASKS

    if args.cmd == "run":
        cmd_run(args, " ".join(args.task))
    elif args.cmd == "demo":
        t = next((t for i, t in enumerate(TASKS, 1) if args.which in (t["id"], str(i))), None)
        if not t:
            sys.exit("Unknown demo. Use `python -m taskwright tasks`.")
        cmd_run(args, t["task"])
    elif args.cmd == "tasks":
        for i, t in enumerate(TASKS, 1):
            print(c(f"{i}. [{t['id']}] ", "b") + t["task"])
            print(c(f"   shows: {t['shows']}", "gr"))
    elif args.cmd == "eval":
        run_eval(args, TASKS)
    elif args.cmd == "sandbox":
        import uvicorn
        print(f"Northstar sandbox on http://127.0.0.1:{args.port}  (mail: /mail  Ledgerly: /erp  handbook: /wiki)")
        uvicorn.run("sandbox.app:app", host="127.0.0.1", port=args.port, log_level="warning")
    elif args.cmd == "reset":
        s = Settings()
        base = args.base_url or s.base_url
        if _healthy(base):          # a running sandbox owns the state: ask it to reset
            _post(base.rstrip("/") + f"/__sandbox/reset?chaos={0 if args.no_chaos else 1}")
        else:
            from sandbox.app import store
            store.reset(chaos=not args.no_chaos)
        print(f"Sandbox reset (chaos {'off' if args.no_chaos else 'on'}).")
        if args.memory:
            from .memory import LessonStore
            LessonStore(s.memory_dir / "lessons.jsonl").clear()
            print("Long-term memory cleared.")
    elif args.cmd == "ui":
        import uvicorn
        from .web import create_app
        s = Settings()
        if args.provider:
            s.provider = args.provider
        if args.model:
            s.model = args.model
        ensure_sandbox(s.base_url)
        print(c(f"Mission control: http://127.0.0.1:{args.port}", "b") + c(f"   (sandbox company at {s.base_url})", "gr"))
        uvicorn.run(create_app(s), host="127.0.0.1", port=args.port, log_level="warning")
    elif args.cmd == "report":
        from pathlib import Path
        from .report import regenerate
        print(regenerate(Path(args.run_dir)))
    elif args.cmd == "memory":
        from .memory import LessonStore
        store = LessonStore(Settings().memory_dir / "lessons.jsonl")
        if args.clear:
            store.clear()
            print("Cleared.")
            return
        lessons = store.all()
        if not lessons:
            print("No lessons yet.")
        for l in lessons:
            print(c(f"[{l.get('kind')}] ", "y") + l["lesson"] + c(f"  ({', '.join(l.get('applies_to', []))}; {l['source_run']})", "gr"))


def run_eval(args, tasks) -> None:
    """Each task runs on a freshly reset sandbox with a scripted human (approves everything,
    answers from the task's script), then is scored against ground truth from the sandbox."""
    from .demo_tasks import check
    s = Settings()
    base = args.base_url or s.base_url
    ensure_sandbox(base, quiet=True)
    rows = []
    for t in tasks:
        if args.only and t["id"] not in args.only:
            continue
        _post(base.rstrip("/") + "/__sandbox/reset?chaos=1")
        res = cmd_run(args, t["task"], answers=t.get("answers"), scripted=True)
        checks = check(t["id"], base, res.get("run_dir"))
        passed = all(ok for _, ok in checks)
        rows.append((t["id"], res, checks, passed))
    print()
    print(c("EVALUATION (ground truth from the sandbox, independent of the agent's own verifier)", "b"))
    agree = 0
    for tid, res, checks, passed in rows:
        verifier_says = res["verification"] == "verified"
        agree += verifier_says == passed
        print(c(f"\n{tid}: ", "b") + (c("PASS", "g", "b") if passed else c("FAIL", "r", "b"))
              + c(f"  · agent status {res['status']} · verifier {res['verification']} · {res['steps']} steps "
                  f"· {res['duration_s']}s", "gr"))
        for name, ok in checks:
            print(f"   {c('✓', 'g') if ok else c('✗', 'r')} {name}")
    if rows:
        print(c(f"\nTasks passed: {sum(r[3] for r in rows)}/{len(rows)} · verifier agreed with ground truth on "
                f"{agree}/{len(rows)}", "b"))


if __name__ == "__main__":
    main()
