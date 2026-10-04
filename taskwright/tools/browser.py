"""Browser control grounded in the DOM.

The agent sees each page as compact text where every interactive element has
a ref ([e12 button "Save invoice"]) and acts on refs. Compared with pixel-level
computer use this is cheaper, deterministic, and lets the runtime inspect
exactly what an action will submit before it happens (see policy.py).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import BrowserContext, Error as PWError, Page, TimeoutError as PWTimeout

from ..human import ApprovalRequest, Human
from ..policy import COMMIT, LOGIN, NAVIGATE, NEUTRAL, READ, Action, PolicyEngine
from ..trace import Trace
from ..vault import SecretError, Vault
from .base import BLOCKED, DENIED, FAILED, INVALID, STALE, TRANSIENT, ToolResult, tool

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text()

DESCRIBE_JS = r"""
(el) => {
  const form = el.form || el.closest('form');
  const tag = el.tagName, type = (el.getAttribute('type') || '').toLowerCase();
  const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim();
  const isLink = tag === 'A' && el.hasAttribute('href');
  const isSubmit = !!form && ((tag === 'BUTTON' && (type === '' || type === 'submit')) ||
                              (tag === 'INPUT' && (type === 'submit' || type === 'image')));
  const out = {tag, type, text, isLink, href: isLink ? el.href : '', download: isLink && el.hasAttribute('download'),
               inForm: !!form, isSubmit, method: '', action: '', fields: {}, labels: {}, hasPassword: false};
  if (form) {
    out.method = (form.getAttribute('method') || 'get').toLowerCase();
    out.action = form.action;
    for (const f of form.elements) {
      if (!f.name || f.disabled) continue;
      const ft = (f.type || '').toLowerCase();
      if (['submit', 'button', 'reset', 'file', 'image'].includes(ft)) continue;
      if ((ft === 'checkbox' || ft === 'radio') && !f.checked) continue;
      if (ft === 'password') { out.hasPassword = true; out.fields[f.name] = f.value ? '********' : ''; continue; }
      out.fields[f.name] = f.tagName === 'SELECT' ? ((f.selectedOptions[0] && f.selectedOptions[0].text) || f.value) : f.value;
      let lab = '';
      if (f.id) { const l = document.querySelector('label[for="' + CSS.escape(f.id) + '"]'); if (l) lab = l.innerText.trim(); }
      out.labels[f.name] = lab || f.getAttribute('aria-label') || '';
    }
  }
  return out;
}
"""

COMMIT_WORDS = re.compile(r"(?i)\b(save|submit|send|delete|remove|pay|approve|confirm|post|publish|place order|"
                          r"transfer|archive|void|create|update|apply|book|reject|sign|accept)\b")


class StaleRef(Exception):
    pass


class BrowserToolkit:
    def __init__(self, context: BrowserContext, *, policy: PolicyEngine, human: Human, vault: Vault,
                 trace: Trace, downloads_dir: Path, shots_dir: Path, base_url: str, label: str = "agent",
                 intent_fn: Callable[[], str] | None = None, highlight: bool = False, max_chars: int = 12000):
        self.context = context
        self.policy, self.human, self.vault, self.trace = policy, human, vault, trace
        self.downloads_dir, self.shots_dir = downloads_dir, shots_dir
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/")
        self.label = label
        self.intent_fn = intent_fn or (lambda: "")
        self.highlight = highlight
        self.max_chars = max_chars
        self._downloads: list = []
        self._nav_status: int | None = None
        self._nav_method: str = "GET"
        self._shot_n = 0
        self._new_tab_note = ""
        self._gate_note = ""
        self.page: Page = context.pages[0] if context.pages else context.new_page()
        self._attach(self.page)
        context.on("page", self._on_new_page)

    # ------------------------------------------------------------------ plumbing

    def _attach(self, page: Page) -> None:
        page.set_default_timeout(10000)
        page.on("download", lambda d: self._downloads.append(d))
        page.on("response", self._on_response)

    def _on_new_page(self, page: Page) -> None:
        self._attach(page)
        self.page = page
        self._new_tab_note = "A new tab opened; you are now working in it."

    def _on_response(self, resp) -> None:
        try:
            if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
                self._nav_status = resp.status
                self._nav_method = resp.request.method
        except Exception:
            pass

    def _settle(self) -> None:
        for state, timeout in (("domcontentloaded", 10000), ("networkidle", 2500)):
            try:
                self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass

    def _snapshot_text(self) -> str:
        res = None
        for _ in range(4):
            try:
                res = self.page.evaluate(SNAPSHOT_JS, self.max_chars)
                break
            except PWError:              # navigation in flight: context destroyed
                time.sleep(0.4)
                self._settle()
        if res is None:
            return f"URL: {self.page.url}\n(page could not be read)"
        try:
            title = self.page.title()
        except PWError:
            title = ""
        head = f"URL: {self.page.url}\nTitle: {title}"
        if self._nav_status and self._nav_status >= 400:
            head += f"\nHTTP status: {self._nav_status}"
        body = res["text"] + ("\n…[snapshot truncated]" if res["truncated"] else "")
        return self.vault.redact(f"{head}\n---\n{body}")

    def _screenshot(self, tag: str = "", full_page: bool = False) -> str | None:
        self._shot_n += 1
        path = self.shots_dir / f"{self.label}-{self._shot_n:03d}{('-' + tag) if tag else ''}.png"
        try:
            self.page.screenshot(path=str(path), full_page=full_page)
            return str(path)
        except Exception:
            return None

    def _observe(self, note: str, extra: list[str] | None = None) -> ToolResult:
        notes = [note] + ([self._gate_note] if self._gate_note else []) + (extra or [])
        self._gate_note = ""
        if self._new_tab_note:
            notes.append(self._new_tab_note)
            self._new_tab_note = ""
        shot = self._screenshot()
        snap = self._snapshot_text()
        # Surface problems the page reports, so they are not lost in a long snapshot.
        alerts = [l[len("[ALERT] "):] for l in snap.splitlines() if l.startswith("[ALERT] ")][:4]
        warning = ""
        if self._nav_status and self._nav_status >= 400:
            warning = f"The server responded with HTTP {self._nav_status}."
        if alerts:
            warning = (warning + " The page shows: " + " ".join(alerts)).strip()
        if warning:
            notes.append("⚠ " + warning)
        header = "\n".join(n for n in notes if n)
        compact = header + "\n" + "\n".join(snap.splitlines()[:2]) + "\n[older page snapshot elided]"
        return ToolResult(content=f"{header}\n\n{snap}", compact=compact,
                          data={"snapshot": True, "url": self.page.url, "screenshot": shot, "warning": warning})

    def _loc(self, ref: str):
        ref = (ref or "").strip().strip("[]")
        if ref.isdigit():
            ref = "e" + ref
        if not re.fullmatch(r"e\d+", ref):
            raise StaleRef(f"'{ref}' is not an element ref. Use refs like e12 from the latest snapshot.")
        loc = self.page.locator(f'[data-tw-ref="{ref}"]')
        if loc.count() == 0:
            raise StaleRef(f"Element {ref} is not on the current page (the page changed since that snapshot). "
                           "Use browser_snapshot to get fresh refs.")
        return loc.first

    def _flash(self, loc) -> None:
        if not self.highlight:
            return
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
            loc.evaluate("e => { e.style.outline = '3px solid #e11d48'; e.style.outlineOffset = '2px'; }")
            time.sleep(0.35)
        except Exception:
            pass

    def _save_downloads(self) -> list[str]:
        notes, seen = [], set()
        while self._downloads:
            d = self._downloads.pop(0)
            if id(d) in seen:
                continue
            seen.add(id(d))
            name = re.sub(r"[^\w.\-]+", "_", d.suggested_filename or "download.bin")
            target = self.downloads_dir / name
            i = 1
            while target.exists():
                target = self.downloads_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
                i += 1
            try:
                d.save_as(str(target))
                rel = f"downloads/{target.name}"
                notes.append(f"Downloaded file saved as '{rel}'. Read it with read_file.")
                self.trace.emit("download", file=rel, url=d.url)
            except Exception as e:
                notes.append(f"A download failed: {e}")
        return notes

    # ------------------------------------------------------------------ policy gate

    def _classify(self, info: dict) -> str:
        if info["isSubmit"]:
            if info["method"] == "get":
                return READ
            if info["hasPassword"]:
                return LOGIN
            return COMMIT
        if info["isLink"]:
            return COMMIT if COMMIT_WORDS.search(info["text"]) and re.search(r"(?i)delete|remove|void|pay", info["text"]) else NAVIGATE
        return COMMIT if COMMIT_WORDS.search(info["text"]) else NEUTRAL

    def _action_from(self, info: dict, kind: str) -> Action:
        fields = {k: self.vault.redact(str(v)) for k, v in info["fields"].items()}
        return Action(kind=kind, url=self.page.url, label=info["text"][:80], fields=fields,
                      field_labels=info["labels"], target=info["action"])

    def _gate(self, action: Action) -> ToolResult | None:
        d = self.policy.evaluate(action)
        if d.effect == "allow":
            return None
        if d.effect == "deny":
            self.trace.emit("policy_block", rule=d.rule_id, reason=d.reason, action=action.summary())
            return ToolResult.error(BLOCKED, f"Blocked by policy [{d.rule_id}]: {d.reason} Do not try to work "
                                             "around this. Adapt your plan, or finish and report it.")
        if self.policy.already_approved(d.rule_id, action):
            self.trace.emit("approval_reused", rule=d.rule_id, action=action.summary())
            self._gate_note = f"This exact entry was already approved by a human earlier [{d.rule_id}]."
            return None
        self.trace.emit("approval_requested", rule=d.rule_id, reason=d.reason, action=action.summary())
        dec = self.human.approve(ApprovalRequest(d.rule_id, d.reason, action, self.intent_fn()))
        self.trace.emit("approval_decision", rule=d.rule_id, approved=dec.approved, note=dec.note, by=dec.by)
        if not dec.approved:
            return ToolResult.error(DENIED, f"A human declined this action [{d.rule_id}]. Their note: "
                                            f"{dec.note or '(none)'}. Do not retry the same action; adjust "
                                            "your plan or finish and report.")
        self.policy.record_approval(d.rule_id, action)
        self._gate_note = (f"Approved by a human before submitting [{d.rule_id}]"
                           + (f"; their note: {dec.note}" if dec.note else "") + ".")
        return None

    # ------------------------------------------------------------------ tools

    @tool("browser_open", "Open a URL in the browser (absolute, or a path like /mail on the company intranet). "
          "Returns the page snapshot.", {"url": {"type": "string"}}, ["url"])
    def browser_open(self, url: str) -> ToolResult:
        url = url.strip()
        if url.startswith("/"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))
        if urlparse(url).scheme not in ("http", "https"):
            return ToolResult.error(INVALID, "Only http(s) URLs can be opened.")
        notes = []
        for attempt in range(1, 4):
            self._nav_status = None
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                status = resp.status if resp else None
            except PWError as e:
                if "Download is starting" in str(e):
                    time.sleep(0.5)
                    return ToolResult("Opened a file download.\n" + "\n".join(self._save_downloads()))
                if attempt < 3:
                    self.trace.emit("retry", tool="browser_open", attempt=attempt, reason=str(e).splitlines()[0])
                    time.sleep(1.5 * attempt)
                    continue
                return ToolResult.error(TRANSIENT, f"Could not open {url}: {str(e).splitlines()[0]}")
            if status and status >= 500 and attempt < 3:
                self.trace.emit("retry", tool="browser_open", attempt=attempt, reason=f"HTTP {status}")
                notes.append(f"HTTP {status} on attempt {attempt}; retried automatically.")
                time.sleep(1.5 * attempt)
                continue
            break
        self._settle()
        return self._observe(f"Opened {url}.", notes + self._save_downloads())

    @tool("browser_snapshot", "Return the current page as text with element refs. Use after anything changes "
          "the page if you need fresh refs.")
    def browser_snapshot(self) -> ToolResult:
        return self._observe("Current page.")

    @tool("browser_click", "Click an element by ref (links, buttons, tabs). Submitting forms or clicking "
          "save/send buttons may trigger a policy check or a human approval automatically. Returns the "
          "resulting page snapshot.", {"ref": {"type": "string", "description": "Element ref, e.g. e12"}},
          ["ref"], idempotent=False)
    def browser_click(self, ref: str) -> ToolResult:
        try:
            loc = self._loc(ref)
        except StaleRef as e:
            return ToolResult.error(STALE, str(e))
        info = loc.evaluate(DESCRIBE_JS)
        kind = self._classify(info)
        action = self._action_from(info, kind)
        if (blocked := self._gate(action)):
            return blocked
        self._flash(loc)
        self._nav_status = None
        try:
            if info["download"]:
                with self.page.expect_download(timeout=15000) as dl:
                    loc.click(timeout=8000)
                if dl.value not in self._downloads:
                    self._downloads.append(dl.value)
            else:
                loc.click(timeout=8000)
        except PWTimeout:
            return ToolResult.error(FAILED, f"Clicking {ref} timed out (element may be hidden, disabled or covered).")
        self._settle()
        notes = []
        if self._nav_status and self._nav_status >= 500:
            # Reloading is only safe if the failing response came from a GET (e.g. the page we were
            # redirected to after a successful POST). A failed POST is never replayed automatically.
            if self._nav_method == "GET":
                notes += self._retry_reload()
            else:
                notes.append(f"The server returned HTTP {self._nav_status} after a state-changing action. It is "
                             "unknown whether the change was applied: check before trying again.")
        desc = f"{info['tag'].lower()} '{info['text'][:60]}'"
        return self._observe(f"Clicked {ref} ({desc}).", notes + self._save_downloads())

    def _retry_reload(self) -> list[str]:
        status = self._nav_status
        for attempt in range(1, 4):
            self.trace.emit("retry", tool="browser_click", attempt=attempt, reason=f"HTTP {status} on a page load")
            time.sleep(1.5 * attempt)
            self._nav_status = None
            try:
                self.page.reload(wait_until="domcontentloaded")
            except PWError:
                continue
            self._settle()
            if not self._nav_status or self._nav_status < 500:
                return [f"The page returned HTTP {status}; the runtime reloaded it automatically and it recovered "
                        f"on retry {attempt}."]
        return [f"The page still returns HTTP {self._nav_status} after 3 automatic retries."]

    @tool("browser_type", "Type text into a text field, or choose an option in a select (give the option text). "
          "Use {{secret:NAME}} for credentials. Set press_enter to submit afterwards.",
          {"ref": {"type": "string"}, "text": {"type": "string"},
           "press_enter": {"type": "boolean", "description": "Press Enter after typing (submits the form)"}},
          ["ref", "text"], idempotent=False)
    def browser_type(self, ref: str, text: str, press_enter: bool = False) -> ToolResult:
        try:
            msg = self._fill_one(ref, text)
        except StaleRef as e:
            return ToolResult.error(STALE, str(e))
        except SecretError as e:
            return ToolResult.error(BLOCKED, str(e))
        except ValueError as e:
            return ToolResult.error(INVALID, str(e))
        except PWError as e:
            return ToolResult.error(INVALID, f"Could not type into {ref}: {str(e).splitlines()[0]}")
        if press_enter:
            return self.browser_press("Enter", _note=msg)
        return ToolResult(msg)

    def _fill_one(self, ref: str, text) -> str:
        loc = self._loc(ref)
        text = "" if text is None else str(text)
        tag = loc.evaluate("e => e.tagName")
        self._flash(loc)
        if tag == "SELECT":
            options = loc.evaluate("e => Array.from(e.options).map(o => [o.text.trim(), o.value])")
            want = text.strip().lower()
            match = [o for o in options if o[0].lower() == want or o[1].lower() == want]
            if not match:
                match = [o for o in options if want and want in o[0].lower()]
            if len(match) != 1:
                listing = "; ".join(o[0] for o in options)
                raise ValueError(f"Option '{text}' {'is ambiguous' if match else 'not found'} in {ref}. Options: {listing}")
            loc.select_option(value=match[0][1])
            return f"Selected '{match[0][0]}' in {ref}."
        typ = (loc.evaluate("e => (e.type || '').toLowerCase()") or "")
        if typ in ("checkbox", "radio"):
            on = text.strip().lower() in ("true", "yes", "on", "1", "checked")
            loc.set_checked(on)
            return f"Set {ref} to {'checked' if on else 'unchecked'}."
        value = self.vault.resolve(text, self.page.url)
        loc.fill(value)
        shown = "********" if typ == "password" else self.vault.redact(text)
        return f"Typed into {ref}: '{shown}'."

    @tool("browser_fill_form", "Fill several fields at once. Each item is a field ref and a value (option text for "
          "selects). Does not submit. Stops at the first field that fails.",
          {"fields": {"type": "array", "items": {"type": "object", "properties": {
              "ref": {"type": "string"}, "value": {"type": "string"}}, "required": ["ref", "value"]}}},
          ["fields"], idempotent=False)
    def browser_fill_form(self, fields: list[dict]) -> ToolResult:
        done = []
        for f in fields or []:
            try:
                done.append(self._fill_one(f.get("ref", ""), f.get("value", "")))
            except (StaleRef, ValueError, SecretError, PWError) as e:
                kind = STALE if isinstance(e, StaleRef) else INVALID
                if isinstance(e, PWError):
                    e = str(e).splitlines()[0]
                prefix = ("Filled so far:\n" + "\n".join(done) + "\n") if done else ""
                return ToolResult.error(kind, f"{prefix}Failed on {f.get('ref')}: {e}")
        return ToolResult("\n".join(done) or "Nothing to fill.")

    @tool("browser_press", "Press a key (e.g. Enter, Tab, Escape, PageDown) on the focused element.",
          {"key": {"type": "string"}}, ["key"], idempotent=False)
    def browser_press(self, key: str, _note: str = "") -> ToolResult:
        if key.lower() in ("enter", "return"):
            key = "Enter"
            try:
                info = self.page.evaluate(f"() => {{ const el = document.activeElement; "
                                          f"return el ? ({DESCRIBE_JS})(el) : null; }}")
            except PWError:
                info = None
            if info and info["inForm"] and info["tag"] != "TEXTAREA":
                info = dict(info, isSubmit=True, text=info["text"] or "Enter")
                action = self._action_from(info, self._classify(info))
                if (blocked := self._gate(action)):
                    return blocked
        self._nav_status = None
        self.page.keyboard.press(key)
        self._settle()
        notes = [_note] if _note else []
        if self._nav_status and self._nav_status >= 500 and self._nav_method == "GET":
            notes += self._retry_reload()
        elif self._nav_status and self._nav_status >= 500:
            notes.append(f"The server returned HTTP {self._nav_status}. If this submitted a form, check whether the "
                         "change was applied before trying again.")
        return self._observe(f"Pressed {key}.", notes + self._save_downloads())

    @tool("browser_back", "Go back to the previous page.")
    def browser_back(self) -> ToolResult:
        try:
            self.page.go_back(wait_until="domcontentloaded")
        except PWError as e:
            return ToolResult.error(FAILED, f"Could not go back: {e}")
        self._settle()
        return self._observe("Went back.")

    @tool("browser_screenshot", "Save a full-page screenshot of the current page as evidence for the final report.",
          {"caption": {"type": "string", "description": "What this screenshot proves"}}, ["caption"])
    def browser_screenshot(self, caption: str) -> ToolResult:
        path = self._screenshot("evidence", full_page=True)
        if not path:
            return ToolResult.error(FAILED, "Screenshot failed.")
        self.trace.emit("evidence", caption=caption, screenshot=path, url=self.page.url, by=self.label)
        return ToolResult(f"Saved screenshot as evidence: {Path(path).name}")
