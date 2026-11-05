"""The same store interface on PostgreSQL + pgvector.

Install the optional driver with `uv sync --extra postgres`, start the database with
`docker compose up -d postgres` (see docker-compose.yml), and set:

    VECTOR_STORE=pgvector
    DATABASE_URL=postgresql://flowpilot:flowpilot@localhost:5433/flowpilot

Schema (created automatically):

- `knowledge_chunks`: one row per chunk. `embedding vector(N)` holds the vector; `tsv` is a generated
  full-text column (title weighted highest, then section, then text) for keyword search.
- An **HNSW** index on `embedding` (approximate nearest neighbour, cosine distance `<=>`) and a **GIN**
  index on `tsv`.
- `knowledge_meta`: which embedder built the index. A different embedder → the table is rebuilt,
  because vectors from two models cannot be compared.

Keyword search here uses PostgreSQL's own ranking (`ts_rank_cd`) instead of BM25. The fused results
differ slightly from MemoryStore; the retrieval eval tells you whether that matters.

This file is exercised by `tests/test_live_pgvector.py`, which only runs when you point it at a
database (it needs Docker, which CI does not have here).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from flowpilot.knowledge.chunking import Chunk
from flowpilot.knowledge.stores import Scored
from flowpilot.knowledge.text import tokenize

_COLUMNS = "id, doc_id, doc_type, title, section, text, source, tags, doc_hash"


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"


def _or_query(query: str) -> str:
    """'appointments still booked' → 'appointment | still | booked' (any word may match, ranked by how many)."""
    words = {w for t in tokenize(query) for w in re.split(r"[^a-z0-9]+", t) if w}
    return " | ".join(sorted(words))


class PgVectorStore:
    def __init__(self, dsn: str) -> None:
        try:
            import psycopg  # noqa: F401  (optional dependency, imported lazily)
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError("VECTOR_STORE=pgvector needs the driver: run `uv sync --extra postgres`") from exc
        self._dsn = dsn
        self._conn: Any = None
        self._dim: int | None = None

    async def _connection(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        return self._conn

    async def _execute(self, sql: str, params: Iterable[Any] | None = None) -> list[tuple]:
        conn = await self._connection()
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall() if cur.description else []

    async def _table_exists(self) -> bool:
        rows = await self._execute("SELECT to_regclass('public.knowledge_chunks') IS NOT NULL")
        return bool(rows[0][0])

    async def prepare(self, embedder_name: str) -> bool:
        await self._execute("CREATE EXTENSION IF NOT EXISTS vector")
        await self._execute("CREATE TABLE IF NOT EXISTS knowledge_meta (key text PRIMARY KEY, value text NOT NULL)")
        rows = await self._execute("SELECT value FROM knowledge_meta WHERE key = 'embedder'")
        compatible = not rows or rows[0][0] == embedder_name
        if not compatible:
            await self._execute("DROP TABLE IF EXISTS knowledge_chunks")
        await self._execute(
            "INSERT INTO knowledge_meta (key, value) VALUES ('embedder', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            [embedder_name],
        )
        return compatible

    async def _ensure_table(self, dim: int) -> None:
        if self._dim == dim:
            return
        await self._execute(
            f"""
            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                id        text PRIMARY KEY,
                doc_id    text NOT NULL,
                doc_type  text NOT NULL,
                title     text NOT NULL,
                section   text NOT NULL,
                text      text NOT NULL,
                source    text NOT NULL,
                tags      text[] NOT NULL DEFAULT '{{}}',
                tags_text text NOT NULL DEFAULT '',
                doc_hash  text NOT NULL,
                embedding vector({dim}) NOT NULL,
                tsv tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('english', title), 'A') ||
                    setweight(to_tsvector('english', section || ' ' || tags_text), 'B') ||
                    setweight(to_tsvector('english', text), 'C')
                ) STORED
            )"""
        )
        await self._execute("CREATE INDEX IF NOT EXISTS knowledge_chunks_doc_idx ON knowledge_chunks (doc_id)")
        await self._execute(
            "CREATE INDEX IF NOT EXISTS knowledge_chunks_embedding_idx ON knowledge_chunks "
            "USING hnsw (embedding vector_cosine_ops)"
        )
        await self._execute("CREATE INDEX IF NOT EXISTS knowledge_chunks_tsv_idx ON knowledge_chunks USING gin (tsv)")
        self._dim = dim

    async def doc_hashes(self) -> dict[str, str]:
        if not await self._table_exists():
            return {}
        rows = await self._execute("SELECT DISTINCT doc_id, doc_hash FROM knowledge_chunks")
        return {doc_id: doc_hash for doc_id, doc_hash in rows}

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("one vector per chunk")
        if not chunks:
            return
        await self._ensure_table(len(vectors[0]))
        conn = await self._connection()
        async with conn.transaction():  # a document is replaced completely or not at all
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM knowledge_chunks WHERE doc_id = ANY(%s)", [sorted({c.doc_id for c in chunks})])
                await cur.executemany(
                    f"INSERT INTO knowledge_chunks ({_COLUMNS}, tags_text, embedding) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                    [
                        (c.id, c.doc_id, c.doc_type, c.title, c.section, c.text, c.source, list(c.tags), c.doc_hash,
                         " ".join(c.tags), _vector_literal(v))
                        for c, v in zip(chunks, vectors, strict=True)
                    ],
                )

    async def delete_docs(self, doc_ids: Iterable[str]) -> None:
        ids = sorted(set(doc_ids))
        if ids and await self._table_exists():
            await self._execute("DELETE FROM knowledge_chunks WHERE doc_id = ANY(%s)", [ids])

    @staticmethod
    def _chunk(row: tuple) -> Chunk:
        id_, doc_id, doc_type, title, section, text, source, tags, doc_hash = row[:9]
        return Chunk(id_, doc_id, doc_type, title, section, text, source, tuple(tags or ()), doc_hash)

    async def vector_search(self, vector: list[float], k: int, doc_type: str | None = None) -> list[Scored]:
        if not await self._table_exists():
            return []
        literal = _vector_literal(vector)
        rows = await self._execute(
            f"SELECT {_COLUMNS}, 1 - (embedding <=> %s::vector) AS score FROM knowledge_chunks "
            "WHERE (%s::text IS NULL OR doc_type = %s) "
            "ORDER BY embedding <=> %s::vector, id LIMIT %s",
            [literal, doc_type, doc_type, literal, k],
        )
        return [Scored(self._chunk(r), float(r[9])) for r in rows if r[9] > 0]

    async def keyword_search(self, query: str, k: int, doc_type: str | None = None) -> list[Scored]:
        expression = _or_query(query)
        if not expression or not await self._table_exists():
            return []
        rows = await self._execute(
            f"SELECT {_COLUMNS}, ts_rank_cd(tsv, q) AS score "
            "FROM knowledge_chunks, to_tsquery('english', %s) AS q "
            "WHERE tsv @@ q AND (%s::text IS NULL OR doc_type = %s) "
            "ORDER BY score DESC, id LIMIT %s",
            [expression, doc_type, doc_type, k],
        )
        return [Scored(self._chunk(r), float(r[9])) for r in rows]

    async def count(self) -> int:
        if not await self._table_exists():
            return 0
        return int((await self._execute("SELECT count(*) FROM knowledge_chunks"))[0][0])

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
