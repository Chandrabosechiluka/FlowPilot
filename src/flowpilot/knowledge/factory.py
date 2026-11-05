"""Build a KnowledgeBase from settings (the one place that knows about every implementation)."""

from __future__ import annotations

from flowpilot.config import KnowledgeSettings
from flowpilot.knowledge.base import KnowledgeBase
from flowpilot.knowledge.embeddings import create_embedder
from flowpilot.knowledge.stores import MemoryStore, VectorStore


def build_knowledge_base(settings: KnowledgeSettings) -> KnowledgeBase:
    embedder = create_embedder(
        settings.embeddings_provider,
        model=settings.embedding_model,
        ollama_url=settings.ollama_url,
        openai_base_url=settings.openai_base_url,
        openai_api_key=settings.openai_api_key,
    )
    store: VectorStore
    if settings.vector_store == "pgvector":
        from flowpilot.knowledge.pgvector_store import PgVectorStore

        assert settings.database_url
        store = PgVectorStore(settings.database_url)
    else:
        store = MemoryStore()
    return KnowledgeBase(store, embedder)
