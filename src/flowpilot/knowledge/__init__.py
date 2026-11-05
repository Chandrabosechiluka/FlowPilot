"""The knowledge base: runbooks and past tickets, searchable by meaning and by keywords (RAG retrieval)."""

from flowpilot.knowledge.base import IngestReport, KnowledgeBase, SearchHit
from flowpilot.knowledge.chunking import Chunk, chunk_document
from flowpilot.knowledge.documents import Document, load_documents
from flowpilot.knowledge.embeddings import Embedder, HashingEmbedder, create_embedder
from flowpilot.knowledge.stores import MemoryStore, VectorStore

__all__ = [
    "Chunk",
    "Document",
    "Embedder",
    "HashingEmbedder",
    "IngestReport",
    "KnowledgeBase",
    "MemoryStore",
    "SearchHit",
    "VectorStore",
    "chunk_document",
    "create_embedder",
    "load_documents",
]
