"""Where chunks and their vectors live, and the two kinds of search over them.

- **Vector search**: cosine similarity between the query's vector and each chunk's vector. Finds
  chunks with similar *meaning* (with a real embedding model).
- **Keyword search**: BM25, the classic ranking behind search engines. Finds chunks containing the
  query's *exact words*: error messages, ids, job names like `no-shows`, HTTP codes like `429`.

Support questions need both: "customers never showed up but still booked" is a meaning question;
"Execution Timeout Expired" is an exact-words question. `KnowledgeBase` fuses the two rankings.

`MemoryStore` keeps everything in RAM and is rebuilt at start-up. For a few hundred chunks that is the
right tool: fast, nothing to operate. `PgVectorStore` (pgvector_store.py) is the same interface on
PostgreSQL, for large corpora or several FlowPilot instances sharing one index.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from flowpilot.knowledge.chunking import Chunk
from flowpilot.knowledge.text import tokenize


@dataclass(frozen=True, slots=True)
class Scored:
    chunk: Chunk
    score: float


class VectorStore(Protocol):
    async def prepare(self, embedder_name: str) -> bool:
        """Get ready for this embedder. Returns False (and empties the store) if the existing index was
        built with a different embedder, because its vectors cannot be compared with new ones."""
        ...

    async def doc_hashes(self) -> dict[str, str]:
        """doc id → content hash of what is indexed now (to skip unchanged documents)."""
        ...

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        """Replace all chunks of the documents these chunks belong to."""
        ...

    async def delete_docs(self, doc_ids: Iterable[str]) -> None: ...

    async def vector_search(self, vector: list[float], k: int, doc_type: str | None = None) -> list[Scored]: ...

    async def keyword_search(self, query: str, k: int, doc_type: str | None = None) -> list[Scored]: ...

    async def count(self) -> int: ...

    async def close(self) -> None: ...


class BM25:
    """Okapi BM25. A term scores higher when it is rare across chunks (IDF) and frequent in this chunk,
    with diminishing returns (k1) and a penalty for long chunks (b)."""

    def __init__(self, documents: list[list[str]], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(doc) for doc in documents]
        self.lengths = [len(doc) for doc in documents]
        self.avg_length = (sum(self.lengths) / len(self.lengths)) if documents else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(documents)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query_terms: list[str]) -> list[float]:
        out = []
        for tf, length in zip(self.tfs, self.lengths, strict=True):
            score = 0.0
            for term in set(query_terms):
                if term not in tf:
                    continue
                freq = tf[term]
                norm = self.k1 * (1 - self.b + self.b * length / (self.avg_length or 1))
                score += self.idf[term] * freq * (self.k1 + 1) / (freq + norm)
            out.append(score)
        return out


class MemoryStore:
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vectors = np.zeros((0, 0), dtype=np.float32)
        self._bm25 = BM25([])
        self._embedder: str | None = None

    async def prepare(self, embedder_name: str) -> bool:
        if self._embedder not in (None, embedder_name):
            self._chunks, self._vectors, self._bm25 = [], np.zeros((0, 0), dtype=np.float32), BM25([])
            self._embedder = embedder_name
            return False
        self._embedder = embedder_name
        return True

    async def doc_hashes(self) -> dict[str, str]:
        return {c.doc_id: c.doc_hash for c in self._chunks}

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk")
        replaced = {c.doc_id for c in chunks}
        keep = [i for i, c in enumerate(self._chunks) if c.doc_id not in replaced]
        new_vectors = np.asarray(vectors, dtype=np.float32) if vectors else None
        old = self._vectors[keep] if keep else None
        parts = [m for m in (old, new_vectors) if m is not None and m.size]
        self._vectors = np.vstack(parts) if parts else np.zeros((0, 0), dtype=np.float32)
        self._chunks = [self._chunks[i] for i in keep] + list(chunks)
        self._reindex()

    async def delete_docs(self, doc_ids: Iterable[str]) -> None:
        gone = set(doc_ids)
        keep = [i for i, c in enumerate(self._chunks) if c.doc_id not in gone]
        self._vectors = self._vectors[keep] if keep else np.zeros((0, 0), dtype=np.float32)
        self._chunks = [self._chunks[i] for i in keep]
        self._reindex()

    def _reindex(self) -> None:
        self._bm25 = BM25([tokenize(c.embedding_text) for c in self._chunks])

    def _allowed(self, doc_type: str | None) -> np.ndarray:
        return np.array([doc_type is None or c.doc_type == doc_type for c in self._chunks], dtype=bool)

    async def vector_search(self, vector: list[float], k: int, doc_type: str | None = None) -> list[Scored]:
        if not self._chunks:
            return []
        sims = self._vectors @ np.asarray(vector, dtype=np.float32)  # vectors are unit length: dot = cosine
        sims = np.where(self._allowed(doc_type), sims, -np.inf)
        order = np.argsort(-sims)[:k]
        return [Scored(self._chunks[i], float(sims[i])) for i in order if np.isfinite(sims[i]) and sims[i] > 0]

    async def keyword_search(self, query: str, k: int, doc_type: str | None = None) -> list[Scored]:
        terms = tokenize(query)
        if not self._chunks or not terms:
            return []
        scores = self._bm25.scores(terms)
        allowed = self._allowed(doc_type)
        ranked = sorted(
            (i for i, s in enumerate(scores) if s > 0 and allowed[i]), key=lambda i: (-scores[i], self._chunks[i].id)
        )
        return [Scored(self._chunks[i], scores[i]) for i in ranked[:k]]

    async def count(self) -> int:
        return len(self._chunks)

    async def close(self) -> None:
        return None
