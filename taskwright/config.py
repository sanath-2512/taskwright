"""Settings, from environment variables (.env) with CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env)
        return
    except ImportError:
        pass
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _company_name() -> str:
    import tomllib
    path = ROOT / "company" / "company.toml"
    return tomllib.loads(path.read_text()).get("name", "the company") if path.exists() else "the company"


@dataclass
class Settings:
    company: str = field(default_factory=_company_name)
    base_url: str = field(default_factory=lambda: os.environ.get("TW_BASE_URL", "http://127.0.0.1:8765"))
    provider: str | None = field(default_factory=lambda: os.environ.get("LLM_PROVIDER") or None)
    model: str | None = field(default_factory=lambda: os.environ.get("LLM_MODEL") or None)
    autonomy: str | None = None                 # None -> config/policies.toml
    max_steps: int = field(default_factory=lambda: int(os.environ.get("TW_MAX_STEPS", "100")))
    verify_rounds: int = 2
    headed: bool = field(default_factory=lambda: os.environ.get("TW_HEADED", "0") == "1")
    slow_mo: int = field(default_factory=lambda: int(os.environ.get("TW_SLOW_MO", "0")))
    # The sandbox company lives on a fixed date so its inbox stays "recent". Override with TW_TODAY.
    today: str = field(default_factory=lambda: os.environ.get("TW_TODAY", "2026-10-04"))
    runs_dir: Path = ROOT / "runs"
    memory_dir: Path = ROOT / "memory"
    handbook_dir: Path = ROOT / "company" / "handbook"
    policies_path: Path = ROOT / "config" / "policies.toml"
    vault_path: Path = ROOT / "config" / "vault.toml"
    learn: bool = True                          # write lessons after each run

    @property
    def today_label(self) -> str:
        d = datetime.strptime(self.today, "%Y-%m-%d").date() if self.today else date.today()
        return d.strftime("%A %d %B %Y")
