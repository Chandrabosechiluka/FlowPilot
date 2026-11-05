"""Settings, read once at start-up from environment variables (and a .env file in development).

C# comparison: this is FlowPilot's `appsettings.json` + `IOptions<T>`. A frozen dataclass is an
immutable record, so nothing can change a setting halfway through a run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import find_dotenv, load_dotenv


class ConfigError(RuntimeError):
    """A required setting is missing or invalid. Raised at start-up, never halfway through a request."""


@dataclass(frozen=True, slots=True)
class Settings:
    garageflow_base_url: str
    garageflow_email: str
    garageflow_password: str
    timeout_seconds: float = 10.0
    max_retries: int = 3
    shop_timezone: str = "Asia/Kolkata"

    def __repr__(self) -> str:  # never print the password into a log
        return (
            f"Settings(garageflow_base_url={self.garageflow_base_url!r}, "
            f"garageflow_email={self.garageflow_email!r}, garageflow_password='***', "
            f"timeout_seconds={self.timeout_seconds}, max_retries={self.max_retries}, "
            f"shop_timezone={self.shop_timezone!r})"
        )


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_settings(*, dotenv: bool = True) -> Settings:
    """Fail fast: a missing setting stops the program at start-up with a clear message."""
    if dotenv:
        # Look for .env from the current folder upwards. Missing .env is fine; real environment variables win.
        load_dotenv(find_dotenv(usecwd=True))

    try:
        timeout = float(os.environ.get("GARAGEFLOW_TIMEOUT_SECONDS", "10"))
        retries = int(os.environ.get("GARAGEFLOW_MAX_RETRIES", "3"))
    except ValueError as exc:
        raise ConfigError(f"GARAGEFLOW_TIMEOUT_SECONDS / GARAGEFLOW_MAX_RETRIES must be numbers: {exc}") from exc
    if timeout <= 0 or not 0 <= retries <= 10:
        raise ConfigError("GARAGEFLOW_TIMEOUT_SECONDS must be > 0 and GARAGEFLOW_MAX_RETRIES between 0 and 10.")

    tz = os.environ.get("SHOP_TIMEZONE", "Asia/Kolkata")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"SHOP_TIMEZONE {tz!r} is not a known IANA time zone.") from exc

    return Settings(
        garageflow_base_url=_required("GARAGEFLOW_BASE_URL").rstrip("/"),
        garageflow_email=_required("GARAGEFLOW_EMAIL"),
        garageflow_password=_required("GARAGEFLOW_PASSWORD"),
        timeout_seconds=timeout,
        max_retries=retries,
        shop_timezone=tz,
    )


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    """Settings for the knowledge base and log search. All optional: the defaults work offline."""

    knowledge_dir: str = "knowledge"
    embeddings_provider: str = "hash"  # hash | ollama | openai
    embedding_model: str | None = None
    ollama_url: str = "http://localhost:11434"
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    vector_store: str = "memory"  # memory | pgvector
    database_url: str | None = None
    log_file: str | None = None  # GarageFlow's console log, e.g. saved with `docker logs -t garageflow-api`

    def __repr__(self) -> str:
        key = "***" if self.openai_api_key else None
        return (
            f"KnowledgeSettings(knowledge_dir={self.knowledge_dir!r}, embeddings_provider={self.embeddings_provider!r}, "
            f"embedding_model={self.embedding_model!r}, vector_store={self.vector_store!r}, "
            f"openai_api_key={key!r}, log_file={self.log_file!r})"
        )


def load_knowledge_settings(*, dotenv: bool = True) -> KnowledgeSettings:
    if dotenv:
        load_dotenv(find_dotenv(usecwd=True))
    env = os.environ
    provider = env.get("EMBEDDINGS_PROVIDER", "hash").strip().lower()
    store = env.get("VECTOR_STORE", "memory").strip().lower()
    if provider not in {"hash", "ollama", "openai"}:
        raise ConfigError(f"EMBEDDINGS_PROVIDER must be hash, ollama or openai, not {provider!r}")
    if store not in {"memory", "pgvector"}:
        raise ConfigError(f"VECTOR_STORE must be memory or pgvector, not {store!r}")
    if store == "pgvector" and not env.get("DATABASE_URL"):
        raise ConfigError("VECTOR_STORE=pgvector needs DATABASE_URL")
    return KnowledgeSettings(
        knowledge_dir=env.get("KNOWLEDGE_DIR", "knowledge"),
        embeddings_provider=provider,
        embedding_model=env.get("EMBEDDING_MODEL") or None,
        ollama_url=env.get("OLLAMA_URL", "http://localhost:11434"),
        openai_base_url=env.get("OPENAI_BASE_URL") or None,
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        vector_store=store,
        database_url=env.get("DATABASE_URL") or None,
        log_file=env.get("GARAGEFLOW_LOG_FILE") or None,
    )
