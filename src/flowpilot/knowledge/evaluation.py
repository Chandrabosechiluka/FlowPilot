"""Measuring retrieval quality, so "better search" is a number, not a feeling.

For each question in `evals/retrieval.json` we know which documents should answer it. Metrics:

- **hit@k**: share of questions where a correct document is in the top k results.
- **MRR** (mean reciprocal rank): 1 if the first result is correct, 1/2 if the second is, ... averaged.
  It rewards putting the right answer *first*, which matters because an agent reads the top results.

Results are reported per `kind`: `wording` questions share words with the answer; `paraphrase`
questions do not. The hashing embedder should do well on the first and poorly on the second; a real
embedding model should close that gap. If it does not, it is not worth its cost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from flowpilot.knowledge.base import KnowledgeBase


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    query: str
    expected: tuple[str, ...]
    kind: str = "wording"


@dataclass
class CaseResult:
    case: EvalCase
    returned: list[str]  # doc ids in rank order (deduplicated)
    rank: int | None  # 1-based rank of the first correct doc, None if missing


@dataclass
class EvalReport:
    k: int
    embedder: str
    results: list[CaseResult] = field(default_factory=list)

    def _subset(self, kind: str | None) -> list[CaseResult]:
        return [r for r in self.results if kind is None or r.case.kind == kind]

    def hit_at(self, n: int, kind: str | None = None) -> float:
        rows = self._subset(kind)
        return sum(1 for r in rows if r.rank is not None and r.rank <= n) / len(rows) if rows else 0.0

    def mrr(self, kind: str | None = None) -> float:
        rows = self._subset(kind)
        return sum(1 / r.rank for r in rows if r.rank) / len(rows) if rows else 0.0

    def table(self) -> str:
        kinds = sorted({r.case.kind for r in self.results})
        lines = [f"Retrieval eval with {self.embedder} (top {self.k})", "", "| set | cases | hit@1 | hit@3 | MRR |", "|---|---|---|---|---|"]
        for kind in [None, *kinds]:
            rows = self._subset(kind)
            lines.append(
                f"| {kind or 'all'} | {len(rows)} | {self.hit_at(1, kind):.0%} | {self.hit_at(3, kind):.0%} | {self.mrr(kind):.2f} |"
            )
        misses = [r for r in self.results if r.rank is None or r.rank > 3]
        if misses:
            lines += ["", "Not in the top 3:"]
            lines += [f"- {r.case.id} ({r.case.kind}) {r.case.query!r}: got {r.returned[:3]}, wanted {list(r.case.expected)}" for r in misses]
        return "\n".join(lines)


def load_cases(path: Path) -> list[EvalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [EvalCase(c["id"], c["query"], tuple(c["expected"]), c.get("kind", "wording")) for c in raw["cases"]]


async def evaluate(kb: KnowledgeBase, cases: list[EvalCase], *, k: int = 5) -> EvalReport:
    report = EvalReport(k=k, embedder=kb.embedder.name)
    for case in cases:
        hits = await kb.search(case.query, k=k * 3)  # chunks; several can come from one document
        docs: list[str] = []
        for h in hits:
            if h.chunk.doc_id not in docs:
                docs.append(h.chunk.doc_id)
        docs = docs[:k]
        rank = next((i for i, d in enumerate(docs, start=1) if d in case.expected), None)
        report.results.append(CaseResult(case, docs, rank))
    return report
