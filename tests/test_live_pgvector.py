"""PgVectorStore against a real PostgreSQL + pgvector, skipped unless you ask for it.

    docker compose up -d postgres
    uv sync --extra postgres
    PowerShell:  $env:PGVECTOR_LIVE="1"; uv run pytest tests/test_live_pgvector.py -v

It drops and recreates the `knowledge_*` tables in the database from DATABASE_URL (default: the
docker-compose one). Do not point it at a database you care about.
"""

from __future__ import annotations

import os
import shutil

import pytest

from flowpilot.knowledge import HashingEmbedder, KnowledgeBase
from flowpilot.knowledge.evaluation import evaluate, load_cases
from tests.conftest import KNOWLEDGE_DIR, ROOT

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("PGVECTOR_LIVE") != "1", reason="set PGVECTOR_LIVE=1 to run against PostgreSQL"),
]

@pytest.fixture
def anyio_backend():
    """psycopg needs the selector event loop on Windows (see flowpilot/compat.py)."""
    import asyncio
    import sys

    return ("asyncio", {"loop_factory": asyncio.SelectorEventLoop}) if sys.platform == "win32" else "asyncio"


DSN = os.environ.get("DATABASE_URL", "postgresql://flowpilot:flowpilot@localhost:5433/flowpilot")


@pytest.fixture
async def store():
    from flowpilot.knowledge.pgvector_store import PgVectorStore

    s = PgVectorStore(DSN)
    await s._execute("DROP TABLE IF EXISTS knowledge_chunks")  # noqa: SLF001
    await s._execute("DROP TABLE IF EXISTS knowledge_meta")  # noqa: SLF001
    yield s
    await s.close()


async def test_ingest_search_and_reingest(store, tmp_path):
    folder = tmp_path / "kb"
    shutil.copytree(KNOWLEDGE_DIR, folder)
    kb = KnowledgeBase(store, HashingEmbedder())

    first = await kb.ingest(folder)
    assert len(first.added) == 18 and await store.count() == first.chunks_embedded

    second = await kb.ingest(folder)
    assert second.chunks_embedded == 0 and second.unchanged == 18  # persisted: nothing re-embedded

    (folder / "tickets" / "T-1049.md").unlink()
    third = await kb.ingest(folder)
    assert third.removed == ["T-1049"]

    hits = await kb.search("Maintenance job failed Execution Timeout Expired", k=3)
    assert hits[0].chunk.doc_id in {"rb-maintenance-jobs", "T-1012"}
    tickets = await kb.search("too many requests login", doc_type="ticket", k=3)
    assert tickets and all(h.chunk.doc_type == "ticket" for h in tickets)


async def test_a_different_embedder_rebuilds(store):
    await KnowledgeBase(store, HashingEmbedder(dim=512)).ingest(KNOWLEDGE_DIR)
    report = await KnowledgeBase(store, HashingEmbedder(dim=256)).ingest(KNOWLEDGE_DIR)
    assert report.rebuilt and len(report.added) == 18


async def test_retrieval_quality_on_postgres(store):
    kb = KnowledgeBase(store, HashingEmbedder())
    await kb.ingest(KNOWLEDGE_DIR)
    report = await evaluate(kb, load_cases(ROOT / "evals" / "retrieval.json"), k=5)
    print(report.table())
    assert report.hit_at(3, "wording") >= 0.85, report.table()
