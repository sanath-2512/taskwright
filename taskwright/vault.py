"""Credential vault: the model handles secret *references*, never secret values."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

PLACEHOLDER = re.compile(r"\{\{\s*secret:([A-Za-z0-9_]+)\s*\}\}")


class SecretError(Exception):
    pass


@dataclass
class Secret:
    name: str
    value: str
    description: str
    allowed_hosts: list[str]
    sensitive: bool = True      # sensitive values are redacted wherever they appear


class Vault:
    def __init__(self, secrets: dict[str, Secret]):
        self.secrets = secrets

    @classmethod
    def load(cls, path: Path) -> "Vault":
        data = tomllib.loads(path.read_text()) if path.exists() else {}
        secrets = {}
        for name, d in data.items():
            # environment variables override the file (e.g. for real deployments)
            value = os.environ.get(f"TW_SECRET_{name}", d.get("value", ""))
            secrets[name] = Secret(name, value, d.get("description", ""), d.get("allowed_hosts", []),
                                   d.get("sensitive", True))
        return cls(secrets)

    def describe(self) -> str:
        if not self.secrets:
            return "No credentials are configured."
        return "\n".join(f"- {{{{secret:{s.name}}}}}: {s.description} (usable on: {', '.join(s.allowed_hosts) or 'any host'})"
                         for s in self.secrets.values())

    def resolve(self, text: str, url: str) -> str:
        """Replace placeholders with values, only if the page's host is allowed for that secret."""
        host = urlparse(url).hostname or ""

        def sub(m):
            s = self.secrets.get(m.group(1))
            if not s:
                raise SecretError(f"Unknown credential '{m.group(1)}'. Use list_credentials to see what exists.")
            if s.allowed_hosts and host not in s.allowed_hosts:
                raise SecretError(f"Credential '{s.name}' may not be used on host '{host}'.")
            return s.value
        return PLACEHOLDER.sub(sub, text)

    def redact(self, text: str) -> str:
        for s in self.secrets.values():
            if s.sensitive and s.value and len(s.value) >= 4:
                text = text.replace(s.value, f"{{{{secret:{s.name}}}}}")
        return text
