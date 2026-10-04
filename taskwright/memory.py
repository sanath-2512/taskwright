"""Memory.

* Working memory (RunState): the brief, the plan, and facts the agent recorded
  with their sources. It is re-rendered into the prompt every turn, so the
  agent does not depend on a long, compacted transcript to remember what it
  found.
* Company knowledge (KnowledgeBase): the handbook, split into sections and
  searchable.
* Long-term memory (LessonStore): lessons distilled after each run (system
  quirks, procedures, clarifications from people) that are retrieved into
  later runs. This is how the worker gets better at *this* company.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .retrieval import BM25, Doc, tokenize


# --------------------------------------------------------------------------- working memory

@dataclass
class RunState:
    task: str
    brief: dict = field(default_factory=dict)
    plan: list[dict] = field(default_factory=list)        # {"step": str, "status": str}
    facts: dict[str, dict] = field(default_factory=dict)  # key -> {"value", "source"}
    clarifications: list[dict] = field(default_factory=list)  # {"question", "answer"}
    lessons: list[dict] = field(default_factory=list)     # retrieved at start
    steps: int = 0
    finished: dict | None = None

    def plan_text(self) -> str:
        marks = {"done": "x", "in_progress": ">", "blocked": "!", "skipped": "-", "pending": " "}
        return "\n".join(f"{i}. [{marks.get(p.get('status', 'pending'), ' ')}] {p['step']}"
                         + (f"  ({p['status']})" if p.get("status") not in (None, "pending") else "")
                         for i, p in enumerate(self.plan, 1)) or "(no plan yet)"

    def facts_text(self) -> str:
        return "\n".join(f"- {k} = {v['value']}  (source: {v.get('source') or 'unspecified'})"
                         for k, v in self.facts.items()) or "(nothing recorded yet)"


# --------------------------------------------------------------------------- company knowledge

class KnowledgeBase:
    def __init__(self, handbook_dir: Path, base_url: str, lessons: "LessonStore | None" = None):
        self.base_url = base_url.rstrip("/")
        self.lessons = lessons
        self.handbook_dir = handbook_dir
        self.sections: list[Doc] = []
        for p in sorted(handbook_dir.glob("*.md")):
            self.sections.extend(self._split(p))
        self.index = BM25(self.sections)

    def _split(self, path: Path) -> list[Doc]:
        text = path.read_text(encoding="utf-8").replace("{{BASE_URL}}", self.base_url)
        title = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), path.stem)
        parts = re.split(r"(?m)^## ", text)
        docs = []
        intro = parts[0].strip()
        if intro:
            docs.append(Doc(f"{path.stem}", intro, {"page": title, "section": "", "url": f"{self.base_url}/wiki/{path.stem}"}))
        for part in parts[1:]:
            head, _, body = part.partition("\n")
            docs.append(Doc(f"{path.stem}#{head.strip()}", f"[{title}] ## {head.strip()}\n{body.strip()}",
                            {"page": title, "section": head.strip(), "url": f"{self.base_url}/wiki/{path.stem}"}))
        return docs

    def pages_for(self, hits: list[tuple[Doc, float]], max_pages: int = 4) -> str:
        """Whole handbook pages for the best hits: procedures are short and their rules interact,
        so planning sees complete pages rather than isolated sections."""
        stems = []
        for d, _ in hits:
            stem = d.id.split("#")[0]
            if stem not in stems:
                stems.append(stem)
        out = []
        for stem in stems[:max_pages]:
            path = self.handbook_dir / f"{stem}.md"
            text = path.read_text(encoding="utf-8").replace("{{BASE_URL}}", self.base_url)
            out.append(f"<source id=\"{stem}\" url=\"{self.base_url}/wiki/{stem}\">\n{text.strip()}\n</source>")
        return "\n\n".join(out) or "No matching handbook pages."

    def search(self, query: str, k: int = 4) -> list[tuple[Doc, float]]:
        return self.index.search(query, k=k, min_score=0.5)

    def render(self, hits: list[tuple[Doc, float]]) -> str:
        if not hits:
            return "No matching handbook sections."
        return "\n\n".join(f"<source id=\"{d.id}\" url=\"{d.meta['url']}\">\n{d.text}\n</source>" for d, _ in hits)


# --------------------------------------------------------------------------- long-term memory

class LessonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def relevant(self, query: str, k: int = 6) -> list[dict]:
        lessons = self.all()
        if not lessons:
            return []
        docs = [Doc(str(i), l["lesson"] + " " + " ".join(l.get("applies_to", [])), l) for i, l in enumerate(lessons)]
        hits = BM25(docs).search(query, k=k, min_score=0.3)
        return [d.meta for d, _ in hits]

    def add(self, lessons: list[dict], run_id: str) -> list[dict]:
        existing = self.all()
        added = []
        for l in lessons:
            if isinstance(l, str):
                l = {"lesson": l}
            if not isinstance(l, dict):
                continue
            text = str(l.get("lesson") or "").strip()
            if not text:
                continue
            if any(_similar(text, e["lesson"]) for e in existing + added):
                continue
            rec = {"lesson": text, "applies_to": l.get("applies_to", []), "kind": l.get("kind", "system_quirk"),
                   "source_run": run_id, "created": datetime.now().isoformat(timespec="seconds")}
            added.append(rec)
        if added:
            with self.path.open("a", encoding="utf-8") as f:
                for rec in added:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return added

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


def _similar(a: str, b: str, threshold: float = 0.6) -> bool:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= threshold
