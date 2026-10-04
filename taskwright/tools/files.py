"""File access, confined to the run's workspace (downloads/ and outputs/)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from ..trace import Trace
from .base import BLOCKED, FAILED, INVALID, NOT_FOUND, ToolResult, tool

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".tsv", ".html", ".xml", ".log", ".yaml", ".yml", ".toml"}


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = []
    for i, p in enumerate(reader.pages, 1):
        try:
            text = p.extract_text(extraction_mode="layout")
        except Exception:
            text = p.extract_text()
        lines = [l.rstrip() for l in (text or "").splitlines()]
        # collapse runs of blank lines
        out, blank = [], 0
        for l in lines:
            blank = blank + 1 if not l.strip() else 0
            if blank <= 1:
                out.append(l)
        pages.append(f"--- page {i} of {len(reader.pages)} ---\n" + "\n".join(out).strip())
    return "\n".join(pages)


class FileToolkit:
    def __init__(self, workspace: Path, trace: Trace, read_only: bool = False):
        self.root = workspace.resolve()
        self.trace = trace
        self.read_only = read_only
        (self.root / "downloads").mkdir(parents=True, exist_ok=True)
        (self.root / "outputs").mkdir(parents=True, exist_ok=True)

    def _path(self, rel: str) -> Path:
        p = (self.root / rel.strip().lstrip("/\\")).resolve()
        if p != self.root and self.root not in p.parents:
            raise PermissionError("Paths must stay inside the workspace (downloads/ or outputs/).")
        return p

    @tool("list_files", "List files in the workspace: downloads/ (files you downloaded) and outputs/ (files you wrote).",
          {"folder": {"type": "string", "description": "Optional: 'downloads' or 'outputs'"}})
    def list_files(self, folder: str = "") -> ToolResult:
        try:
            base = self._path(folder or ".")
        except PermissionError as e:
            return ToolResult.error(BLOCKED, str(e))
        files = [p for p in sorted(base.rglob("*")) if p.is_file()]
        if not files:
            return ToolResult("No files yet.")
        return ToolResult("\n".join(f"{p.relative_to(self.root).as_posix()}  ({p.stat().st_size} bytes)" for p in files))

    @tool("read_file", "Read a workspace file. PDFs are converted to text (layout preserved); CSV and text files "
          "are returned as text.", {"path": {"type": "string", "description": "e.g. downloads/document.pdf"}}, ["path"])
    def read_file(self, path: str) -> ToolResult:
        try:
            p = self._path(path)
        except PermissionError as e:
            return ToolResult.error(BLOCKED, str(e))
        if not p.exists():
            hint = ""
            matches = [q for q in self.root.rglob("*") if q.is_file() and q.name == Path(path).name]
            if matches:
                hint = f" Did you mean '{matches[0].relative_to(self.root).as_posix()}'?"
            return ToolResult.error(NOT_FOUND, f"No such file: {path}.{hint}")
        try:
            if p.suffix.lower() == ".pdf":
                text = pdf_text(p)
                if not text.strip():
                    return ToolResult.error(FAILED, "The PDF has no extractable text (it may be a scan).")
            elif p.suffix.lower() in TEXT_SUFFIXES:
                text = p.read_text(encoding="utf-8", errors="replace")
            else:
                return ToolResult.error(INVALID, f"Unsupported file type '{p.suffix}'.")
        except Exception as e:
            return ToolResult.error(FAILED, f"Could not read {path}: {e}")
        if len(text) > 15000:
            text = text[:15000] + "\n…[truncated]"
        self.trace.emit("file_read", path=path)
        return ToolResult(f"Contents of {path}:\n{text}", compact=f"[contents of {path} elided; read it again if needed]")

    @tool("write_file", "Create or overwrite a file in outputs/ (for reports, CSVs and other deliverables).",
          {"path": {"type": "string", "description": "e.g. outputs/report.csv"}, "content": {"type": "string"}},
          ["path", "content"], idempotent=True)
    def write_file(self, path: str, content: str) -> ToolResult:
        if self.read_only:
            return ToolResult.error(BLOCKED, "This session is read-only.")
        if not path.replace("\\", "/").startswith("outputs/"):
            path = "outputs/" + path.lstrip("/\\")
        try:
            p = self._path(path)
        except PermissionError as e:
            return ToolResult.error(BLOCKED, str(e))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        note = ""
        if p.suffix.lower() == ".csv":
            try:
                rows = list(csv.reader(io.StringIO(content)))
                note = f" Parsed as CSV: {len(rows)} rows (including header), {len(rows[0]) if rows else 0} columns."
            except csv.Error as e:
                note = f" Warning: not valid CSV ({e})."
        self.trace.emit("file_written", path=path, bytes=len(content.encode()))
        return ToolResult(f"Wrote {path} ({len(content)} characters).{note}")
