"""Shared fixtures. Everything runs in-process: the sandbox app is plugged into the HTTP client
through ASGITransport, so no port is opened and no network is used."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import httpx2 as httpx
import pytest
from mcp import Client

from flowpilot.config import Settings
from flowpilot.garageflow import GarageFlowClient
from flowpilot.knowledge import HashingEmbedder, KnowledgeBase, MemoryStore
from flowpilot.logs import LogStore
from flowpilot.mcp_server import create_server
from flowpilot.sandbox import SandboxState, create_app

# 12:00 in Hyderabad. Seed data and "now" are pinned so every run sees the same data.
NOW = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
BASE_URL = "http://garageflow.test"
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def log_file(tmp_path) -> Path:
    return tmp_path / "garageflow.log"


@pytest.fixture
def sandbox(log_file):
    return create_app(now=NOW, log_path=log_file)


@pytest.fixture
async def knowledge() -> KnowledgeBase:
    kb = KnowledgeBase(MemoryStore(), HashingEmbedder())
    await kb.ingest(KNOWLEDGE_DIR)
    return kb


@pytest.fixture
def logs(log_file, sandbox) -> LogStore:  # depends on sandbox: it writes the file
    return LogStore(log_file)


@pytest.fixture
def sandbox_state(sandbox) -> SandboxState:
    return sandbox.state.sandbox


def make_settings(email: str = "advisor@garageflow.local", password: str = "Garage@12345", retries: int = 3) -> Settings:
    return Settings(garageflow_base_url=BASE_URL, garageflow_email=email, garageflow_password=password, max_retries=retries)


class SleepRecorder:
    """Stands in for anyio.sleep so retry tests are instant, and records the delays asked for."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
async def gf(sandbox, sleeps) -> AsyncIterator[GarageFlowClient]:
    async with GarageFlowClient(make_settings(), transport=httpx.ASGITransport(app=sandbox), sleep=sleeps) as client:
        yield client


@pytest.fixture
async def admin_gf(sandbox, sleeps) -> AsyncIterator[GarageFlowClient]:
    async with GarageFlowClient(
        make_settings(email="admin@garageflow.local"), transport=httpx.ASGITransport(app=sandbox), sleep=sleeps
    ) as client:
        yield client


async def _mcp(client: GarageFlowClient, knowledge: KnowledgeBase, logs: LogStore) -> AsyncIterator[Client]:
    server = create_server(client=client, knowledge=knowledge, logs=logs)
    server.runtime.now = lambda: NOW
    async with Client(server) as mcp_client:
        yield mcp_client


@pytest.fixture
async def mcp(gf, knowledge, logs) -> AsyncIterator[Client]:
    async for c in _mcp(gf, knowledge, logs):
        yield c


@pytest.fixture
async def admin_mcp(admin_gf, knowledge, logs) -> AsyncIterator[Client]:
    async for c in _mcp(admin_gf, knowledge, logs):
        yield c
