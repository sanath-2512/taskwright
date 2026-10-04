"""The simulated company: an AP mailbox, the Ledgerly AP system and the handbook.

Run with:  python -m taskwright sandbox     (or: uvicorn sandbox.app:app --port 8765)

This is the environment the agent works in. It is a real web application that
the agent operates through a real browser; the agent has no back door into it.
Deliberate "chaos" (session expiry mid-submit, a transient 503) is built in so
recovery behaviour can be demonstrated. Disable with SANDBOX_CHAOS=0.
"""

from __future__ import annotations

import copy
import html
import json
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import markdown
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import seed
from .pdfgen import render_invoice

ROOT = Path(__file__).resolve().parent.parent
HANDBOOK_DIR = ROOT / "company" / "handbook"
STATE_DIR = Path(os.environ.get("SANDBOX_STATE_DIR", ROOT / ".sandbox_state"))
SESSION_COOKIE = "ledgerly_session"


# --------------------------------------------------------------------------- state

class Store:
    """State lives in memory (one lock) and is persisted to a JSON file on every write, so it survives
    restarts. Reads never touch the disk, which keeps concurrent requests safe on every OS."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.lock = threading.RLock()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state: dict = {}
        if self.path.exists():
            try:
                self.state = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.state = {}
        if not self.state or not (self.dir / "attachments").exists():
            self.reset()

    def reset(self, chaos: bool | None = None) -> None:
        if chaos is None:
            chaos = os.environ.get("SANDBOX_CHAOS", "1") != "0"
        att_dir = self.dir / "attachments"
        att_dir.mkdir(parents=True, exist_ok=True)
        for key, inv in seed.INVOICES.items():
            render_invoice(inv, att_dir / inv["file"])
        state = {
            "ledger": copy.deepcopy(seed.LEDGER),
            "next_id": 106,
            "sent": [],
            "sessions": [],
            "chaos": {"enabled": chaos, "session_expiry_armed": True, "outage_armed": True},
            "log": [],
        }
        with self.lock:
            self.state = state
            self._persist()

    def read(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.state)

    def _persist(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        os.replace(tmp, self.path)

    def update(self, fn):
        with self.lock:
            result = fn(self.state)
            self._persist()
            return result


store = Store(STATE_DIR)
app = FastAPI(title="Northstar sandbox", docs_url=None, redoc_url=None)

VENDOR_BY_CODE = {v["code"]: v for v in seed.VENDORS}
ATTACHMENTS = {key: inv for key, inv in seed.INVOICES.items()}


def log_event(kind: str, **data) -> None:
    def fn(s):
        s["log"].append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data})
    store.update(fn)


# --------------------------------------------------------------------------- layout

CSS = """
*{box-sizing:border-box} body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d2433;background:#f4f5f7}
header{display:flex;align-items:center;gap:24px;padding:0 24px;height:52px;color:#fff}
header .brand{font-weight:700;font-size:16px;letter-spacing:.2px}
header nav a{color:#fff;opacity:.85;margin-right:16px;text-decoration:none}
header nav a:hover{opacity:1;text-decoration:underline}
header .who{margin-left:auto;opacity:.85;font-size:13px}
main{max-width:1040px;margin:24px auto;padding:0 24px}
.card{background:#fff;border:1px solid #dfe3ea;border-radius:8px;padding:20px 24px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 14px} h2{font-size:16px;margin:18px 0 8px}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eceff3;vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.4px;color:#5b6475;background:#fafbfc}
a{color:#1a5fd0} .muted{color:#6b7385} .right{text-align:right}
.btn{display:inline-block;background:#1a5fd0;color:#fff;border:0;border-radius:6px;padding:8px 14px;font-size:14px;cursor:pointer;text-decoration:none}
.btn.secondary{background:#e9edf3;color:#1d2433}
label{display:block;font-weight:600;margin:12px 0 4px} input,select,textarea{width:100%;padding:8px 10px;border:1px solid #c9cfd9;border-radius:6px;font:inherit}
textarea{min-height:140px} .row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.alert{border-radius:6px;padding:10px 14px;margin-bottom:14px} .alert.err{background:#fdecec;border:1px solid #f3b5b5;color:#8a1c1c}
.alert.ok{background:#e8f6ee;border:1px solid #a9dcbc;color:#165c34} .alert.warn{background:#fff6e0;border:1px solid #f0d58c;color:#6b4e00}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.Pending{background:#fff3cd;color:#7a5a00}.Approved{background:#dbeafe;color:#1e40af}.Paid{background:#dcfce7;color:#166534}
.body{white-space:pre-wrap;font-family:inherit;background:#fafbfc;border:1px solid #eceff3;border-radius:6px;padding:14px}
dl{display:grid;grid-template-columns:170px 1fr;gap:6px 12px;margin:0} dt{color:#5b6475} dd{margin:0}
.search{display:flex;gap:8px;margin-bottom:14px} .search input{max-width:320px}
"""

APPS = {
    "mail": ("Northstar Mail", "#0f6e6e", [("Inbox", "/mail"), ("Sent", "/mail?folder=sent"), ("Compose", "/mail/compose")]),
    "erp": ("Ledgerly AP", "#3b2f8f", [("Invoices", "/erp/invoices"), ("New invoice", "/erp/invoices/new"), ("Vendors", "/erp/vendors")]),
    "wiki": ("Northstar Handbook", "#7a3d12", [("All pages", "/wiki")]),
    "home": ("Northstar Intranet", "#26313f", [("Mail", "/mail"), ("Ledgerly", "/erp"), ("Handbook", "/wiki")]),
}


def page(app_key: str, title: str, body: str, who: str = "", status: int = 200) -> HTMLResponse:
    name, color, nav = APPS[app_key]
    links = "".join(f'<a href="{href}">{label}</a>' for label, href in nav)
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)} - {name}</title><style>{CSS}</style></head>
<body><header style="background:{color}"><span class="brand">{name}</span><nav>{links}</nav>
<span class="who">{html.escape(who)}</span></header><main>{body}</main></body></html>"""
    return HTMLResponse(doc, status_code=status)


def esc(x) -> str:
    return html.escape(str(x))


def fmt_inr(x: float) -> str:
    from .pdfgen import _inr
    return _inr(float(x))


# --------------------------------------------------------------------------- home

@app.get("/", response_class=HTMLResponse)
def home():
    body = """<div class="card"><h1>Northstar Retail intranet</h1>
<p>Welcome. Tools for the Accounts Payable team:</p>
<ul><li><a href="/mail">Mail</a> - shared AP mailbox (ap@northstar.example)</li>
<li><a href="/erp">Ledgerly</a> - AP system of record</li>
<li><a href="/wiki">Handbook</a> - procedures and vendor directory</li></ul></div>"""
    return page("home", "Home", body)


# --------------------------------------------------------------------------- mail

def _all_messages(state: dict) -> list[dict]:
    inbox = [dict(m, folder="inbox", to="ap@northstar.example") for m in seed.EMAILS]
    sent = [dict(m, folder="sent") for m in state["sent"]]
    return inbox + sent


@app.get("/mail", response_class=HTMLResponse)
def mail_list(folder: str = "inbox", q: str = ""):
    state = store.read()
    msgs = [m for m in _all_messages(state) if m["folder"] == folder]
    if q:
        ql = q.lower()
        msgs = [m for m in msgs if ql in (m["from_name"] + " " + m["from_email"] + " " + m.get("to", "") + " "
                                          + m["subject"] + " " + m["body"]).lower()]
    msgs.sort(key=lambda m: m["date"], reverse=True)
    rows = []
    for m in msgs:
        who = m["from_name"] if folder == "inbox" else "To: " + m["to"]
        clip = " 📎" if m.get("attachments") else ""
        rows.append(f'<tr><td>{esc(who)}<div class="muted">{esc(m["from_email"] if folder=="inbox" else "")}</div></td>'
                    f'<td><a href="/mail/m/{m["id"]}">{esc(m["subject"])}</a>{clip}</td>'
                    f'<td class="muted">{esc(m["date"])}</td></tr>')
    table = ("<table><thead><tr><th>From</th><th>Subject</th><th>Received</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>") if rows else '<p class="muted">No messages.</p>'
    title = "Inbox" if folder == "inbox" else "Sent"
    search = f"""<form class="search" method="get" action="/mail" role="search">
<input type="hidden" name="folder" value="{esc(folder)}">
<input name="q" aria-label="Search mail" placeholder="Search mail" value="{esc(q)}">
<button class="btn secondary" type="submit">Search</button></form>"""
    note = f'<p class="muted">Showing results for "{esc(q)}". <a href="/mail?folder={esc(folder)}">Clear search</a></p>' if q else ""
    body = f'<div class="card"><h1>{title} ({len(msgs)})</h1>{search}{note}{table}</div>'
    return page("mail", title, body, who="ap@northstar.example")


@app.get("/mail/m/{mid}", response_class=HTMLResponse)
def mail_view(mid: str, sent: int = 0):
    state = store.read()
    msg = next((m for m in _all_messages(state) if m["id"] == mid), None)
    if not msg:
        return page("mail", "Not found", '<div class="card"><h1>Message not found</h1></div>', status=404)
    banner = '<div class="alert ok" role="status">Message sent.</div>' if sent else ""
    atts = ""
    if msg.get("attachments"):
        items = []
        for key in msg["attachments"]:
            f = ATTACHMENTS[key]["file"]
            size = (store.dir / "attachments" / f).stat().st_size // 1024 + 1
            items.append(f'<li><a href="/mail/attachments/{key}" download="{esc(f)}">{esc(f)}</a> '
                         f'<span class="muted">(PDF, {size} KB)</span></li>')
        atts = f'<h2>Attachments</h2><ul>{"".join(items)}</ul>'
    actions = ""
    if msg["folder"] == "inbox":
        actions = f'<p><a class="btn" href="/mail/compose?reply_to={mid}">Reply</a></p>'
    sender = f'{esc(msg["from_name"])} &lt;{esc(msg["from_email"])}&gt;'
    body = f"""<div class="card">{banner}<h1>{esc(msg["subject"])}</h1>
<dl><dt>From</dt><dd>{sender}</dd><dt>To</dt><dd>{esc(msg["to"])}</dd>
<dt>Date</dt><dd>{esc(msg["date"])}</dd></dl>
<h2>Message</h2><div class="body">{esc(msg["body"])}</div>{atts}{actions}</div>"""
    return page("mail", msg["subject"], body, who="ap@northstar.example")


@app.get("/mail/attachments/{key}")
def mail_attachment(key: str):
    inv = ATTACHMENTS.get(key)
    if not inv:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = store.dir / "attachments" / inv["file"]
    return FileResponse(path, media_type="application/pdf", filename=inv["file"],
                        content_disposition_type="attachment")


@app.get("/mail/compose", response_class=HTMLResponse)
def mail_compose(reply_to: str = "", to: str = "", subject: str = ""):
    state = store.read()
    body_text = ""
    if reply_to:
        orig = next((m for m in _all_messages(state) if m["id"] == reply_to), None)
        if orig:
            to = orig["from_email"]
            subject = orig["subject"] if orig["subject"].lower().startswith("re:") else "Re: " + orig["subject"]
            quoted = "\n".join("> " + line for line in orig["body"].splitlines())
            body_text = f"\n\n\nOn {orig['date']}, {orig['from_name']} wrote:\n{quoted}"
    return page("mail", "Compose", _compose_form(to, subject, body_text, reply_to), who="ap@northstar.example")


def _compose_form(to, subject, body_text, reply_to, error: str = "") -> str:
    err = f'<div class="alert err" role="alert">{esc(error)}</div>' if error else ""
    return f"""<div class="card"><h1>New message</h1>{err}
<form method="post" action="/mail/compose">
<input type="hidden" name="reply_to" value="{esc(reply_to)}">
<label for="to">To</label><input id="to" name="to" value="{esc(to)}">
<label for="subject">Subject</label><input id="subject" name="subject" value="{esc(subject)}">
<label for="body">Message</label><textarea id="body" name="body">{esc(body_text)}</textarea>
<p><button class="btn" type="submit">Send</button>
<a class="btn secondary" href="/mail">Discard</a></p></form></div>"""


@app.post("/mail/compose", response_class=HTMLResponse)
def mail_send(to: str = Form(""), subject: str = Form(""), body: str = Form(""), reply_to: str = Form("")):
    to = to.strip()
    if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", to):
        return page("mail", "Compose", _compose_form(to, subject, body, reply_to,
                    "Enter a single valid recipient email address."), status=422)
    if not subject.strip():
        return page("mail", "Compose", _compose_form(to, subject, body, reply_to, "Subject is required."), status=422)

    def fn(s):
        sid = f"s{len(s['sent']) + 1:02d}"
        s["sent"].append({"id": sid, "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
                          "from_name": "Accounts Payable", "from_email": "ap@northstar.example",
                          "to": to, "subject": subject.strip(), "body": body, "attachments": [],
                          "in_reply_to": reply_to})
        return sid
    sid = store.update(fn)
    log_event("mail_sent", id=sid, to=to, subject=subject)
    return RedirectResponse(f"/mail/m/{sid}?sent=1", status_code=303)


# --------------------------------------------------------------------------- Ledgerly (erp)

def _session_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    for s in store.read()["sessions"]:
        if s["token"] == token:
            return s["user"]
    return None


def _login_redirect(next_url: str, expired: bool = False) -> RedirectResponse:
    q = f"?next={quote(next_url)}" + ("&expired=1" if expired else "")
    return RedirectResponse("/erp/login" + q, status_code=303)


@app.get("/erp")
def erp_root(request: Request):
    return RedirectResponse("/erp/invoices" if _session_user(request) else "/erp/login", status_code=303)


@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(next: str = "/erp/invoices", expired: int = 0, error: str = ""):
    alert = ""
    if expired:
        alert = '<div class="alert warn" role="alert">Your session has expired. Please sign in again.</div>'
    if error:
        alert = f'<div class="alert err" role="alert">{esc(error)}</div>'
    body = f"""<div class="card" style="max-width:420px;margin:40px auto"><h1>Sign in to Ledgerly</h1>{alert}
<form method="post" action="/erp/login"><input type="hidden" name="next" value="{esc(next)}">
<label for="username">Username</label><input id="username" name="username" autocomplete="username">
<label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password">
<p><button class="btn" type="submit">Sign in</button></p></form></div>"""
    return page("erp", "Sign in", body)


@app.post("/erp/login")
def erp_login(username: str = Form(""), password: str = Form(""), next: str = Form("/erp/invoices")):
    if seed.LEDGERLY_USERS.get(username.strip()) != password:
        return RedirectResponse(f"/erp/login?next={quote(next)}&error={quote('Invalid username or password.')}",
                                status_code=303)
    token = secrets.token_hex(16)
    store.update(lambda s: s["sessions"].append({"token": token, "user": username.strip()}))
    if not next.startswith("/erp"):
        next = "/erp/invoices"
    resp = RedirectResponse(next, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True)
    return resp


@app.get("/erp/logout")
def erp_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    store.update(lambda s: s.__setitem__("sessions", [x for x in s["sessions"] if x["token"] != token]))
    return RedirectResponse("/erp/login", status_code=303)


def _vendor_label(code: str) -> str:
    v = VENDOR_BY_CODE.get(code)
    return f"{code} · {v['name']}" if v else code


@app.get("/erp/invoices", response_class=HTMLResponse)
def erp_list(request: Request, q: str = "", status: str = ""):
    user = _session_user(request)
    if not user:
        return _login_redirect(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
    state = store.read()
    if state["chaos"]["enabled"] and state["chaos"]["outage_armed"]:
        store.update(lambda s: s["chaos"].__setitem__("outage_armed", False))
        log_event("chaos_outage")
        return page("erp", "Unavailable", '<div class="card"><h1>503 Service Unavailable</h1>'
                    '<p role="alert">Ledgerly is temporarily unavailable. Please try again in a moment.</p></div>',
                    who=user, status=503)
    rows = state["ledger"]
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["invoice_number"] + " " + _vendor_label(r["vendor"]) + " " + r["id"]).lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    rows = sorted(rows, key=lambda r: r["id"], reverse=True)
    trs = "".join(
        f'<tr><td><a href="/erp/invoices/{r["id"]}">{r["id"]}</a></td><td>{esc(_vendor_label(r["vendor"]))}</td>'
        f'<td>{esc(r["invoice_number"])}</td><td>{esc(r["invoice_date"])}</td><td>{esc(r["due_date"])}</td>'
        f'<td class="right">{fmt_inr(r["amount"])}</td><td><span class="badge {r["status"]}">{r["status"]}</span></td></tr>'
        for r in rows)
    opts = "".join(f'<option value="{s}"{" selected" if s == status else ""}>{s}</option>'
                   for s in ["Pending", "Approved", "Paid"])
    body = f"""<div class="card"><h1>Invoices ({len(rows)})</h1>
<form class="search" method="get" action="/erp/invoices" role="search">
<input name="q" aria-label="Search invoices" placeholder="Vendor, invoice no. or record ID" value="{esc(q)}">
<select name="status" aria-label="Status" style="max-width:160px"><option value="">All statuses</option>{opts}</select>
<button class="btn secondary" type="submit">Filter</button>
<a class="btn" href="/erp/invoices/new" style="margin-left:auto">New invoice</a></form>
<table><thead><tr><th>Record</th><th>Vendor</th><th>Invoice no.</th><th>Invoice date</th><th>Due date</th>
<th class="right">Amount (INR)</th><th>Status</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return page("erp", "Invoices", body, who=f"Signed in as {user}")


def _invoice_form(values: dict, errors: list[str]) -> str:
    err = ""
    if errors:
        err = '<div class="alert err" role="alert"><strong>Could not save invoice:</strong><ul>' + \
              "".join(f"<li>{esc(e)}</li>" for e in errors) + "</ul></div>"
    vopts = '<option value="">Select vendor…</option>' + "".join(
        f'<option value="{v["code"]}"{" selected" if values.get("vendor") == v["code"] else ""}>'
        f'{esc(_vendor_label(v["code"]))}</option>' for v in seed.VENDORS)
    g = lambda k: esc(values.get(k, ""))
    return f"""<div class="card"><h1>Record a vendor invoice</h1>{err}
<form method="post" action="/erp/invoices/new">
<label for="vendor">Vendor</label><select id="vendor" name="vendor">{vopts}</select>
<div class="row"><div><label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" value="{g('invoice_number')}"></div>
<div><label for="po_number">PO number</label><input id="po_number" name="po_number" value="{g('po_number')}"></div></div>
<div class="row"><div><label for="invoice_date">Invoice date</label><input id="invoice_date" name="invoice_date" value="{g('invoice_date')}"></div>
<div><label for="due_date">Due date</label><input id="due_date" name="due_date" value="{g('due_date')}"></div></div>
<label for="amount">Amount (INR)</label><input id="amount" name="amount" value="{g('amount')}">
<label for="notes">Notes</label><textarea id="notes" name="notes" style="min-height:70px">{g('notes')}</textarea>
<p><button class="btn" type="submit">Save invoice</button> <a class="btn secondary" href="/erp/invoices">Cancel</a></p>
</form></div>"""


@app.get("/erp/invoices/new", response_class=HTMLResponse)
def erp_new_form(request: Request):
    user = _session_user(request)
    if not user:
        return _login_redirect("/erp/invoices/new")
    return page("erp", "New invoice", _invoice_form({}, []), who=f"Signed in as {user}")


def _parse_date(s: str):
    s = s.strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", s):
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y")
    except ValueError:
        return None


@app.post("/erp/invoices/new")
def erp_create(request: Request, vendor: str = Form(""), invoice_number: str = Form(""),
               invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
               po_number: str = Form(""), notes: str = Form("")):
    user = _session_user(request)
    if not user:
        return _login_redirect("/erp/invoices/new", expired=True)
    state = store.read()
    if state["chaos"]["enabled"] and state["chaos"]["session_expiry_armed"]:
        token = request.cookies.get(SESSION_COOKIE)
        def expire(s):
            s["chaos"]["session_expiry_armed"] = False
            s["sessions"] = [x for x in s["sessions"] if x["token"] != token]
        store.update(expire)
        log_event("chaos_session_expired")
        return _login_redirect("/erp/invoices/new", expired=True)

    values = dict(vendor=vendor, invoice_number=invoice_number.strip(), invoice_date=invoice_date.strip(),
                  due_date=due_date.strip(), amount=amount.strip(), po_number=po_number.strip(), notes=notes)
    errors = []
    if vendor not in VENDOR_BY_CODE:
        errors.append("Vendor: select a vendor from the list.")
    if not values["invoice_number"]:
        errors.append("Invoice number is required.")
    d_inv, d_due = _parse_date(values["invoice_date"]), _parse_date(values["due_date"])
    for label, raw, parsed in [("Invoice date", values["invoice_date"], d_inv), ("Due date", values["due_date"], d_due)]:
        if parsed is None:
            errors.append(f"{label}: '{raw}' is not a valid date. Use the format DD/MM/YYYY (for example 30/09/2026).")
    if d_inv and d_due and d_due < d_inv:
        errors.append("Due date cannot be before the invoice date.")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", values["amount"]):
        errors.append(f"Amount: '{values['amount']}' is not valid. Enter numbers only, without commas or "
                      "currency symbols (for example 52280.00).")
    elif float(values["amount"]) <= 0:
        errors.append("Amount must be greater than zero.")
    dup = next((r for r in state["ledger"] if r["vendor"] == vendor
                and r["invoice_number"].lower() == values["invoice_number"].lower()), None)
    if dup:
        errors.append(f"Duplicate: invoice {dup['invoice_number']} from this vendor is already recorded as {dup['id']}.")
    if errors:
        log_event("invoice_rejected", errors=errors)
        return page("erp", "New invoice", _invoice_form(values, errors), who=f"Signed in as {user}", status=422)

    def create(s):
        rid = f"AP-{s['next_id']:06d}"
        s["next_id"] += 1
        s["ledger"].append({"id": rid, "vendor": vendor, "invoice_number": values["invoice_number"],
                            "invoice_date": values["invoice_date"], "due_date": values["due_date"],
                            "amount": round(float(values["amount"]), 2), "po_number": values["po_number"],
                            "status": "Pending", "payment_date": "", "notes": notes.strip(),
                            "created_by": user, "created_at": datetime.now().isoformat(timespec="seconds")})
        return rid
    rid = store.update(create)
    log_event("invoice_created", id=rid, invoice_number=values["invoice_number"])
    return RedirectResponse(f"/erp/invoices/{rid}?created=1", status_code=303)


@app.get("/erp/invoices/{rid}", response_class=HTMLResponse)
def erp_detail(request: Request, rid: str, created: int = 0):
    user = _session_user(request)
    if not user:
        return _login_redirect(f"/erp/invoices/{rid}")
    r = next((x for x in store.read()["ledger"] if x["id"] == rid), None)
    if not r:
        return page("erp", "Not found", '<div class="card"><h1>Invoice record not found</h1></div>', who=user, status=404)
    banner = f'<div class="alert ok" role="status">Invoice recorded as {rid}.</div>' if created else ""
    pay_label = {"Paid": "Payment date", "Approved": "Scheduled payment date"}.get(r["status"], "Payment date")
    body = f"""<div class="card">{banner}<h1>{rid} · {esc(r['invoice_number'])}</h1>
<dl><dt>Vendor</dt><dd>{esc(_vendor_label(r['vendor']))}</dd>
<dt>Invoice number</dt><dd>{esc(r['invoice_number'])}</dd>
<dt>Invoice date</dt><dd>{esc(r['invoice_date'])}</dd><dt>Due date</dt><dd>{esc(r['due_date'])}</dd>
<dt>Amount (INR)</dt><dd>{fmt_inr(r['amount'])}</dd><dt>PO number</dt><dd>{esc(r['po_number'] or '-')}</dd>
<dt>Status</dt><dd><span class="badge {r['status']}">{r['status']}</span></dd>
<dt>{pay_label}</dt><dd>{esc(r['payment_date'] or 'Not scheduled yet')}</dd>
<dt>Notes</dt><dd>{esc(r['notes'] or '-')}</dd><dt>Recorded by</dt><dd>{esc(r['created_by'])}</dd></dl>
<p><a href="/erp/invoices">← Back to invoices</a></p></div>"""
    return page("erp", rid, body, who=f"Signed in as {user}")


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    user = _session_user(request)
    if not user:
        return _login_redirect("/erp/vendors")
    trs = "".join(f"<tr><td>{v['code']}</td><td>{esc(v['name'])}</td><td>{esc(v['billing_email'])}</td>"
                  f"<td>{v['terms']}</td></tr>" for v in seed.VENDORS)
    body = f"""<div class="card"><h1>Vendors</h1><table><thead><tr><th>Code</th><th>Name</th>
<th>Billing email</th><th>Terms</th></tr></thead><tbody>{trs}</tbody></table></div>"""
    return page("erp", "Vendors", body, who=f"Signed in as {user}")


# --------------------------------------------------------------------------- handbook

def _handbook_pages() -> list[tuple[str, str, str]]:
    pages = []
    for p in sorted(HANDBOOK_DIR.glob("*.md")):
        text = p.read_text()
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), p.stem)
        pages.append((p.stem, title, text))
    return pages


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index():
    items = "".join(f'<li><a href="/wiki/{slug}">{esc(title)}</a></li>' for slug, title, _ in _handbook_pages())
    return page("wiki", "Handbook", f'<div class="card"><h1>Handbook</h1><ul>{items}</ul></div>')


@app.get("/wiki/{slug}", response_class=HTMLResponse)
def wiki_page(slug: str, request: Request):
    for s, title, text in _handbook_pages():
        if s == slug:
            base = str(request.base_url).rstrip("/")
            rendered = markdown.markdown(text.replace("{{BASE_URL}}", base), extensions=["tables"])
            return page("wiki", title, f'<div class="card">{rendered}</div>')
    return page("wiki", "Not found", '<div class="card"><h1>Page not found</h1></div>', status=404)


# --------------------------------------------------------------------------- sandbox admin (not linked; tests only)

@app.get("/__sandbox/health")
def health():
    return {"ok": True}


@app.post("/__sandbox/reset")
def reset(chaos: int = 1):
    store.reset(chaos=bool(chaos))
    return {"ok": True, "chaos": bool(chaos)}


@app.get("/__sandbox/state")
def dump_state():
    s = store.read()
    s.pop("sessions", None)
    return s
