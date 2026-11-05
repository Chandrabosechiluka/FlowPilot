"""Embedders: text in, a list of numbers (a vector) out. Similar meaning → vectors pointing the same way.

Three implementations behind one interface, chosen by `EMBEDDINGS_PROVIDER`:

| Provider | What | When |
|---|---|---|
| `hash` | Feature hashing of words and word pairs. No model, no network, instant, deterministic. | Tests, CI, offline demos. It matches *words*, not meaning. |
| `ollama` | A real embedding model running locally (e.g. `nomic-embed-text`). Free, private. | Your laptop; customers who cannot send data out. |
| `openai` | Any OpenAI-compatible `/embeddings` API (OpenAI, Azure OpenAI, Gemini's OpenAI endpoint...). | Cloud deployments. |

The rest of FlowPilot only sees `Embedder`, so swapping providers is a configuration change, and the
retrieval evaluation (`flowpilot-kb eval`) tells you whether the swap actually helped.

Important: vectors from different models are not comparable. Changing the model means re-indexing
everything (the stores record which embedder built the index).
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Protocol

import httpx2 as httpx

from flowpilot.knowledge.text import tokenize


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    @property
    def name(self) -> str:
        """Identifies the model, e.g. 'ollama:nomic-embed-text'. Stored with the index."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in the same order. Vectors are L2-normalised (length 1)."""
        ...


def normalise(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(v * v for v in vector))
    return [v / length for v in vector] if length else vector


class HashingEmbedder:
    """The "hashing trick": each word (and each pair of neighbouring words) is hashed to one of `dim`
    positions, with a sign, weighted by log(1 + count). No vocabulary to store, no training.

    It is honest about what it is: a lexical baseline. "DB unreachable" and "database down" share no
    words, so it cannot match them. A real model can. That gap is exactly what the eval measures.
    """

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    @property
    def name(self) -> str:
        return f"hash:{self.dim}"

    def _vector(self, text: str) -> list[float]:
        tokens = tokenize(text)
        features = Counter(tokens) + Counter(f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False))
        vector = [0.0] * self.dim
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * math.log1p(count)
        return normalise(vector)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]


class _HttpEmbedder:
    batch_size = 32

    def __init__(self, *, model: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60.0) -> None:
        self.model = model
        self._transport = transport
        self._timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(transport=self._transport, timeout=self._timeout) as http:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                try:
                    got = await self._embed_batch(http, batch)
                except httpx.TransportError as exc:
                    raise EmbeddingError(f"{self.name}: could not reach the embedding service ({type(exc).__name__})") from exc
                if len(got) != len(batch):
                    raise EmbeddingError(f"{self.name}: asked for {len(batch)} vectors, got {len(got)}")
                vectors.extend(normalise(v) for v in got)
        return vectors

    @property
    def name(self) -> str:
        raise NotImplementedError

    async def _embed_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        raise NotImplementedError


class OllamaEmbedder(_HttpEmbedder):
    """Ollama's `POST /api/embed` with a list of inputs. Run `ollama pull nomic-embed-text` once first."""

    def __init__(self, *, url: str = "http://localhost:11434", model: str = "nomic-embed-text", **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.url = url.rstrip("/")

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    async def _embed_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        response = await http.post(f"{self.url}/api/embed", json={"model": self.model, "input": batch})
        if response.status_code != 200:
            raise EmbeddingError(f"{self.name}: HTTP {response.status_code}: {response.text[:200]}")
        return response.json()["embeddings"]


class OpenAICompatibleEmbedder(_HttpEmbedder):
    """`POST {base_url}/embeddings` as defined by OpenAI and copied by many providers."""

    def __init__(self, *, base_url: str, api_key: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key

    @property
    def name(self) -> str:
        return f"openai:{self.model}"

    async def _embed_batch(self, http: httpx.AsyncClient, batch: list[str]) -> list[list[float]]:
        response = await http.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": batch},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if response.status_code != 200:
            raise EmbeddingError(f"{self.name}: HTTP {response.status_code}: {response.text[:200]}")
        data = sorted(response.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def create_embedder(
    provider: str,
    *,
    model: str | None = None,
    ollama_url: str = "http://localhost:11434",
    openai_base_url: str | None = None,
    openai_api_key: str | None = None,
) -> Embedder:
    """Factory: configuration decides the class, callers only see `Embedder`."""
    match provider:
        case "hash":
            return HashingEmbedder()
        case "ollama":
            return OllamaEmbedder(url=ollama_url, model=model or "nomic-embed-text")
        case "openai":
            if not openai_api_key or not openai_base_url:
                raise EmbeddingError("EMBEDDINGS_PROVIDER=openai needs OPENAI_BASE_URL and OPENAI_API_KEY")
            return OpenAICompatibleEmbedder(
                base_url=openai_base_url, api_key=openai_api_key, model=model or "text-embedding-3-small"
            )
        case _:
            raise EmbeddingError(f"Unknown EMBEDDINGS_PROVIDER {provider!r}: use hash, ollama or openai")
