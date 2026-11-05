"""Knowledge base: documents, chunking, embeddings, stores, hybrid search, idempotent ingest, retrieval eval."""

from __future__ import annotations

import json
import shutil

import httpx2 as httpx
import pytest

from flowpilot.knowledge import HashingEmbedder, KnowledgeBase, MemoryStore, chunk_document, load_documents
from flowpilot.knowledge.documents import DocumentError, parse_document
from flowpilot.knowledge.embeddings import EmbeddingError, OllamaEmbedder, OpenAICompatibleEmbedder, create_embedder
from flowpilot.knowledge.evaluation import evaluate, load_cases
from flowpilot.knowledge.stores import BM25
from flowpilot.knowledge.text import tokenize
from tests.conftest import KNOWLEDGE_DIR, ROOT

pytestmark = pytest.mark.anyio

DOC = """---
id: rb-test
title: Test runbook
type: runbook
tags: [alpha, beta]   # comment
---

# Test runbook

Intro line.

## Diagnose

Check the thing.

### Deeper

More detail.

## Fix

Do the fix.
"""


# ------------------------------------------------------------------ documents


def test_front_matter_is_parsed():
    doc = parse_document(DOC, source="x.md")
    assert (doc.id, doc.title, doc.type, doc.tags) == ("rb-test", "Test runbook", "runbook", ("alpha", "beta"))


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("no header", "must start with"),
        ("---\nid: a\n", "not closed"),
        ("---\nid: a\ntitle: t\n---\nbody", "missing type"),
        ("---\nid: a\ntitle: t\ntype: recipe\n---\nbody", "type must be"),
    ],
)
def test_malformed_documents_name_the_problem(text, error):
    with pytest.raises(DocumentError, match=error):
        parse_document(text, source="bad.md")


def test_duplicate_ids_are_refused(tmp_path):
    (tmp_path / "a.md").write_text(DOC, encoding="utf-8")
    (tmp_path / "b.md").write_text(DOC, encoding="utf-8")
    with pytest.raises(DocumentError, match="already used"):
        load_documents(tmp_path)


def test_the_real_corpus_loads():
    docs = load_documents(KNOWLEDGE_DIR)
    assert {d.type for d in docs} == {"runbook", "ticket"}
    assert len([d for d in docs if d.type == "ticket"]) == 10


def test_content_hash_changes_with_content():
    a = parse_document(DOC, source="x.md")
    b = parse_document(DOC.replace("Do the fix.", "Do another fix."), source="x.md")
    assert a.content_hash != b.content_hash


# ------------------------------------------------------------------- chunking


def test_chunks_follow_headings_and_keep_the_path():
    chunks = chunk_document(parse_document(DOC, source="x.md"))
    assert [c.section for c in chunks] == ["Overview", "Diagnose", "Diagnose > Deeper", "Fix"]
    assert chunks[0].id == "rb-test#0"
    assert chunks[2].embedding_text.startswith("Test runbook\nSection: Diagnose > Deeper")
    assert chunks[3].citation == "x.md (Fix)"


def test_long_sections_are_split_with_overlap():
    paragraphs = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(10))
    text = f"---\nid: long\ntitle: Long\ntype: runbook\n---\n\n## Big\n\n{paragraphs}\n"
    chunks = chunk_document(parse_document(text, source="l.md"), max_chars=800)
    assert len(chunks) > 2
    assert all(len(c.text) <= 1200 for c in chunks)
    last_para = chunks[0].text.split("\n\n")[-1]
    assert chunks[1].text.startswith(last_para)  # overlap


def test_headings_inside_code_blocks_are_not_sections():
    text = "---\nid: c\ntitle: C\ntype: runbook\n---\n\n## Real\n\n```\n# not a heading\n```\n"
    assert [c.section for c in chunk_document(parse_document(text, source="c.md"))] == ["Real"]


def test_a_ticket_is_one_chunk_without_its_title_heading():
    docs = {d.id: d for d in load_documents(KNOWLEDGE_DIR)}
    [chunk] = chunk_document(docs["T-1012"])
    assert chunk.section == "Ticket"
    assert "## Root cause" in chunk.text and not chunk.text.startswith("# ")


# ----------------------------------------------------------------- embeddings


def test_tokenize_drops_stop_words_and_plural_s():
    assert tokenize("The appointments are NOT marked as no-shows") == ["appointment", "marked", "no-show"]


async def test_hashing_embedder_is_deterministic_and_unit_length():
    e = HashingEmbedder()
    [a, b] = await e.embed(["database unreachable", "database unreachable"])
    assert a == b
    assert abs(sum(v * v for v in a) - 1) < 1e-9


async def test_similar_texts_are_closer_than_unrelated_ones():
    e = HashingEmbedder()
    q, near, far = await e.embed(["maintenance job failed", "the maintenance job failed again", "customer phone number"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731
    assert dot(q, near) > dot(q, far)


async def test_ollama_embedder_calls_the_embed_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0], [0.0, 2.0]]})

    e = OllamaEmbedder(url="http://ollama:11434/", transport=httpx.MockTransport(handler))
    vectors = await e.embed(["a", "b"])
    assert seen["url"] == "http://ollama:11434/api/embed"
    assert seen["body"] == {"model": "nomic-embed-text", "input": ["a", "b"]}
    assert vectors == [[0.6, 0.8], [0.0, 1.0]]  # normalised


async def test_openai_compatible_embedder_orders_by_index_and_sends_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]})

    e = OpenAICompatibleEmbedder(base_url="https://api.example/v1", api_key="sk-test", model="m",
                                 transport=httpx.MockTransport(handler))
    assert await e.embed(["first", "second"]) == [[1.0, 0.0], [0.0, 1.0]]


async def test_embedding_service_errors_are_clear():
    e = OllamaEmbedder(transport=httpx.MockTransport(lambda r: httpx.Response(404, text="model not found")))
    with pytest.raises(EmbeddingError, match="HTTP 404"):
        await e.embed(["x"])


def test_embedder_factory():
    assert create_embedder("hash").name == "hash:512"
    assert create_embedder("ollama", model="mxbai-embed-large").name == "ollama:mxbai-embed-large"
    with pytest.raises(EmbeddingError, match="OPENAI_BASE_URL"):
        create_embedder("openai")
    with pytest.raises(EmbeddingError, match="Unknown"):
        create_embedder("magic")


# --------------------------------------------------------------------- stores


def test_bm25_prefers_rare_terms():
    bm25 = BM25([["timeout", "job"], ["job", "ok"], ["job", "fine"]])
    scores = bm25.scores(["timeout", "job"])
    assert scores[0] > scores[1] > 0


async def test_memory_store_filters_by_type(knowledge):
    hits = await knowledge.search("maintenance job failed", doc_type="ticket", k=5)
    assert hits and all(h.chunk.doc_type == "ticket" for h in hits)


# ------------------------------------------------------------- ingest + search


async def test_ingest_is_idempotent_and_tracks_changes(tmp_path):
    folder = tmp_path / "kb"
    shutil.copytree(KNOWLEDGE_DIR, folder)
    kb = KnowledgeBase(MemoryStore(), HashingEmbedder())

    first = await kb.ingest(folder)
    assert len(first.added) == 18 and first.chunks_embedded == await kb.store.count()

    again = await kb.ingest(folder)
    assert (again.added, again.updated, again.unchanged, again.chunks_embedded) == ([], [], 18, 0)

    page = folder / "runbooks" / "time-zones.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n## Extra\n\nNew advice.\n", encoding="utf-8")
    (folder / "tickets" / "T-1049.md").unlink()
    changed = await kb.ingest(folder)
    assert changed.updated == ["rb-time-zones"] and changed.removed == ["T-1049"]
    assert "T-1049" not in await kb.store.doc_hashes()


async def test_changing_the_embedder_rebuilds_the_index():
    store = MemoryStore()
    await KnowledgeBase(store, HashingEmbedder(dim=512)).ingest(KNOWLEDGE_DIR)
    report = await KnowledgeBase(store, HashingEmbedder(dim=256)).ingest(KNOWLEDGE_DIR)
    assert report.rebuilt and len(report.added) == 18


async def test_hybrid_search_finds_the_runbook_for_a_log_error(knowledge):
    hits = await knowledge.search("Maintenance job 'no-shows' failed SqlException Execution Timeout Expired", k=3)
    assert hits[0].chunk.doc_id in {"rb-maintenance-jobs", "T-1012"}
    assert hits[0].vector_rank and hits[0].keyword_rank  # found by both


async def test_empty_query_returns_nothing(knowledge):
    assert await knowledge.search("   ") == []


async def test_retrieval_quality_does_not_regress(knowledge):
    """A quality gate: if a change to chunking or ranking makes retrieval worse, this fails."""
    report = await evaluate(knowledge, load_cases(ROOT / "evals" / "retrieval.json"), k=5)
    assert report.hit_at(3, "wording") >= 0.9, report.table()
    assert report.mrr() >= 0.8, report.table()
