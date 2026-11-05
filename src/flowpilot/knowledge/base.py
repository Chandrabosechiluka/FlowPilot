"""The knowledge base: ingest documents, then hybrid search with Reciprocal Rank Fusion.

**Ingest** is idempotent: each document's content hash is compared with what is indexed, so only new
or changed documents are re-chunked and re-embedded, and deleted files are removed from the index.
With a paid embedding API this is the difference between paying once and paying on every start-up.

**Hybrid search** runs vector search and keyword search, then fuses the two rankings with Reciprocal
Rank Fusion (RRF): each chunk scores `sum(1 / (60 + rank))` over the lists it appears in. RRF only uses
ranks, so it does not care that cosine similarities (0..1) and BM25 scores (0..20+) are on different
scales, which is why it is the usual first choice for hybrid retrieval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from flowpilot.knowledge.chunking import Chunk, chunk_document
from flowpilot.knowledge.documents import load_documents
from flowpilot.knowledge.embeddings import Embedder
from flowpilot.knowledge.stores import VectorStore

RRF_K = 60
CANDIDATES = 20  # how deep each list goes before fusion


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk: Chunk
    score: float  # fused RRF score (higher is better; only meaningful within one result list)
    vector_rank: int | None  # 1-based rank in vector search, None if not in its top candidates
    keyword_rank: int | None


@dataclass
class IngestReport:
    embedder: str
    documents: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    chunks_embedded: int = 0
    rebuilt: bool = False  # True when the embedder changed and everything was re-embedded
    seconds: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.documents} documents: {len(self.added)} added, {len(self.updated)} updated, "
            f"{self.unchanged} unchanged, {len(self.removed)} removed; {self.chunks_embedded} chunks embedded "
            f"with {self.embedder} in {self.seconds:.1f}s" + (" (full rebuild: embedder changed)" if self.rebuilt else "")
        )


class KnowledgeBase:
    def __init__(self, store: VectorStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    async def ingest(self, folder: Path) -> IngestReport:
        started = time.perf_counter()
        report = IngestReport(embedder=self.embedder.name)
        report.rebuilt = not await self.store.prepare(self.embedder.name)

        docs = load_documents(folder)
        report.documents = len(docs)
        indexed = await self.store.doc_hashes()

        changed = [d for d in docs if indexed.get(d.id) != d.content_hash]
        for d in changed:
            (report.updated if d.id in indexed else report.added).append(d.id)
        report.unchanged = len(docs) - len(changed)

        chunks = [c for d in changed for c in chunk_document(d)]
        if chunks:
            vectors = await self.embedder.embed([c.embedding_text for c in chunks])
            await self.store.upsert(chunks, vectors)
            report.chunks_embedded = len(chunks)

        current = {d.id for d in docs}
        report.removed = sorted(set(indexed) - current)
        if report.removed:
            await self.store.delete_docs(report.removed)

        report.seconds = time.perf_counter() - started
        return report

    async def search(self, query: str, *, k: int = 5, doc_type: str | None = None) -> list[SearchHit]:
        query = query.strip()
        if not query:
            return []
        [vector] = await self.embedder.embed([query])
        by_vector = await self.store.vector_search(vector, CANDIDATES, doc_type)
        by_keyword = await self.store.keyword_search(query, CANDIDATES, doc_type)

        vector_rank = {s.chunk.id: n for n, s in enumerate(by_vector, start=1)}
        keyword_rank = {s.chunk.id: n for n, s in enumerate(by_keyword, start=1)}
        chunks = {s.chunk.id: s.chunk for s in [*by_vector, *by_keyword]}

        def fused(chunk_id: str) -> float:
            return sum(1 / (RRF_K + ranks[chunk_id]) for ranks in (vector_rank, keyword_rank) if chunk_id in ranks)

        ordered = sorted(chunks, key=lambda cid: (-fused(cid), cid))[:k]
        return [SearchHit(chunks[cid], fused(cid), vector_rank.get(cid), keyword_rank.get(cid)) for cid in ordered]
