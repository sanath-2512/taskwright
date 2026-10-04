"""Small BM25 index. Deterministic, dependency-free and inspectable: for a
handbook of this size, lexical retrieval is easier to debug than embeddings.
(Swapping in an embedding index only changes this file.)"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

STOP = set("""a an and are as at be by for from has have i in is it its of on or our that the this to was were
will with we you your me my do does did can could should would please then than so if not no yes into
when what which who whom how all any each every there their them they he she his her us""".split())


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+(?:[-/][a-z0-9]+)*", text.lower())
    out = []
    for t in toks:
        if t in STOP:
            continue
        out.append(t)
        if "-" in t or "/" in t:      # also index the parts of INV-2026-0912, ap-3 ...
            out.extend(p for p in re.split(r"[-/]", t) if p and p not in STOP)
        if len(t) > 4 and t.endswith("s"):  # crude plural folding
            out.append(t[:-1])
    return out


@dataclass
class Doc:
    id: str
    text: str
    meta: dict = field(default_factory=dict)


class BM25:
    def __init__(self, docs: list[Doc], k1: float = 1.4, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf = [Counter(tokenize(d.text)) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if docs else 0
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 5, min_score: float = 0.0) -> list[tuple[Doc, float]]:
        q = set(tokenize(query))
        scored = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                f = self.tf[i].get(t)
                if not f:
                    continue
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1)))
            if s > min_score:
                scored.append((d, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
