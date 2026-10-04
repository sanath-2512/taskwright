# Code tour

A suggested reading order. The agent is about 3,400 lines (Python plus the snapshot script); the sandbox company is about 900.

1. **`taskwright/runtime.py`**: one run, top to bottom.
   - `_understand` builds the brief.
   - `_execute_and_verify` launches the browser, runs the worker loop, runs the verifier, and feeds a
     failed verdict back for rework.
   - `_reflect` distils lessons.
   - The report is written at the end of `execute`.
2. **`taskwright/loop.py`**: the tool-calling loop used by both the worker and the verifier. Read
   `run` (one turn = model call, tool calls, observations) and `dispatch` (validation, idempotent
   retries, redaction, tracing), then `_stall_check` and `_compacted`.
3. **`taskwright/tools/browser.py`** and **`snapshot.js`**: how a page becomes text with element refs.
   `browser_click` shows the full sequence for one action:
   - locate the element,
   - describe exactly what it would submit,
   - classify the action,
   - pass the policy gate (and approval if needed),
   - act,
   - settle,
   - retry a failed GET reload, but never a POST,
   - collect downloads,
   - observe.
4. **`taskwright/policy.py`** with **`config/policies.toml`**: allow / approve / deny. Approvals are
   fingerprinted on the normalised payload.
5. **`taskwright/vault.py`**: secret placeholders, host scoping, redaction.
6. **`taskwright/memory.py`**:
   - `RunState` is the working memory re-rendered every turn.
   - `KnowledgeBase` is handbook retrieval.
   - `LessonStore` is long-term memory.
7. **`taskwright/prompts.py`**: the planning, worker, verifier and reflection prompts, and the state block.
8. **`taskwright/llm.py`**: two wire formats (Anthropic, OpenAI-compatible), retry and backoff, forced
   structured output.
9. **`taskwright/tools/agent_tools.py`**: `remember`, `update_plan`, `ask_human`, `finish`,
   `search_knowledge`, `report_verdict`.
10. **`taskwright/web.py`** and **`web_ui.html`**: mission control. `WebHuman` blocks the worker thread
    until the browser answers.
11. **`sandbox/app.py`**: the company the worker operates in. Search for `chaos` to see the injected
    failures.

## Following one event through the system

Take the moment the worker clicks **Save invoice** on a ₹52,280 invoice:

1. The model returns `browser_click(ref="e11")`.
2. `AgentLoop.dispatch` validates the arguments and calls `BrowserToolkit.browser_click`.
3. `DESCRIBE_JS` reads the form: `vendor=V-1001 · Acme…`, `amount=52280.00`, and so on. `_classify`
   says `commit`.
4. `PolicyEngine.evaluate` matches rule `ap-high-value-invoice` (amount ≥ 50000), so the result is
   `approve`.
5. The payload fingerprint has not been approved yet, so `Human.approve()` runs. That is the terminal
   prompt or the browser modal.
6. On approval the fingerprint is recorded, the click happens, and the page settles. If the session
   expired, the snapshot shows `[ALERT] Your session has expired` and `_observe` puts that alert at the
   top of the result.
7. The loop traces `tool_result` with the warning and screenshot. The console, mission control and the
   report all render from that event.
8. Next turn, the model sees the alert and signs in again. It refills the form, and the resubmission
   matches the approved fingerprint, so there is no second prompt.
