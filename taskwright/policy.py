"""Runtime-enforced action policy.

The browser toolkit describes every action *before* performing it (what kind
of action, on which URL, with exactly which form values). The policy engine
decides allow / require approval / deny. Because this happens in code at the
moment of action, a confused or prompt-injected model cannot talk its way
past it.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# action kinds produced by the browser toolkit's classifier
NAVIGATE = "navigate"   # following a link
READ = "read"           # GET form (search, filter)
LOGIN = "login"         # form with a password field
COMMIT = "commit"       # anything that changes state: POST forms, save/send/delete buttons
NEUTRAL = "neutral"     # buttons that do not look state-changing (tabs, toggles)


@dataclass
class Action:
    kind: str
    url: str
    label: str = ""                       # button / link text
    fields: dict[str, str] = field(default_factory=dict)   # form field name -> value (secrets redacted)
    field_labels: dict[str, str] = field(default_factory=dict)  # field name -> human label
    target: str = ""                      # form action URL, if any

    def summary(self) -> str:
        lines = [f"{self.kind.upper()} '{self.label}' on {self.url}"]
        for k, v in self.fields.items():
            lab = self.field_labels.get(k) or k
            if v:
                lines.append(f"  {lab}: {v}")
        return "\n".join(lines)


@dataclass
class Decision:
    effect: str                  # allow | approve | deny
    rule_id: str = ""
    reason: str = ""


@dataclass
class Rule:
    id: str
    description: str
    effect: str
    action: str | None = None
    url_contains: str | None = None
    label_matches: str | None = None
    field: str | None = None
    gte: float | None = None
    lte: float | None = None
    contains: str | None = None
    not_contains: str | None = None
    matches: str | None = None
    present: bool | None = None

    def applies(self, a: Action) -> bool:
        if self.action and a.kind != self.action:
            return False
        if self.url_contains and self.url_contains not in (a.target or "") and self.url_contains not in a.url:
            return False
        if self.label_matches and not re.search(self.label_matches, a.label or ""):
            return False
        if self.field:
            val = _field(a, self.field)
            if self.present is not None:
                return (val not in (None, "")) == self.present
            if val is None:
                return False
            if self.gte is not None or self.lte is not None:
                num = _number(val)
                if num is None:
                    return False
                if self.gte is not None and num < self.gte:
                    return False
                if self.lte is not None and num > self.lte:
                    return False
            if self.contains is not None and self.contains.lower() not in val.lower():
                return False
            if self.not_contains is not None and self.not_contains.lower() in val.lower():
                return False
            if self.matches is not None and not re.search(self.matches, val):
                return False
        return True


def _field(a: Action, name: str) -> str | None:
    for k, v in a.fields.items():
        if k.lower() == name.lower():
            return v
    return None


def _canonical(v: str) -> str:
    from datetime import datetime
    s = (v or "").strip()
    if re.fullmatch(r"[₹$]?\s*(INR\s*)?[\d,]+(\.\d+)?", s):
        return f"{float(re.sub(r'[^\d.]', '', s)):.2f}"
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return re.sub(r"\s+", " ", s).lower()


def _number(s: str) -> float | None:
    s = re.sub(r"[^\d.\-]", "", s or "")
    try:
        return float(s)
    except ValueError:
        return None


class PolicyEngine:
    def __init__(self, rules: list[Rule], autonomy: str = "standard", read_only: bool = False):
        self.rules = rules
        self.autonomy = autonomy
        self.read_only = read_only
        self._approved: set[str] = set()

    @classmethod
    def load(cls, path: Path, autonomy: str | None = None, read_only: bool = False) -> "PolicyEngine":
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        rules = [Rule(**r) for r in data.get("rules", [])]
        return cls(rules, autonomy or data.get("autonomy", "standard"), read_only)

    def evaluate(self, a: Action) -> Decision:
        if a.kind not in (COMMIT,):
            return Decision("allow")
        if self.read_only:
            return Decision("deny", "read-only", "This session is read-only (verification). State-changing actions are not allowed.")
        matched = [r for r in self.rules if r.applies(a)]
        for r in matched:
            if r.effect == "deny":
                return Decision("deny", r.id, r.description)
        if self.autonomy == "autonomous":
            return Decision("allow")
        for r in matched:
            if r.effect == "approve":
                return Decision("approve", r.id, r.description)
        if self.autonomy == "supervised":
            return Decision("approve", "supervised-mode", "Supervised mode: every state-changing action needs approval.")
        return Decision("allow")

    # Approvals are bound to the exact payload. Re-submitting identical data
    # (e.g. after a session expiry) does not ask twice; any change does.
    @staticmethod
    def fingerprint(rule_id: str, a: Action) -> str:
        # Normalised so that re-typing the same meaning in another format ("52,280.00" vs "52280",
        # "2026-09-30" vs "30/09/2026") is recognised as the same approved payload.
        payload = {"rule": rule_id, "target": (a.target or a.url).split("?")[0], "label": a.label,
                   "fields": {k: _canonical(v) for k, v in sorted(a.fields.items()) if (v or "").strip()}}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def already_approved(self, rule_id: str, a: Action) -> bool:
        return self.fingerprint(rule_id, a) in self._approved

    def record_approval(self, rule_id: str, a: Action) -> None:
        self._approved.add(self.fingerprint(rule_id, a))
