"""Shared fixtures. Everything runs in-process: the sandbox app is plugged into the HTTP client
through ASGITransport, so no port is opened and no network is used."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx2 as httpx
import pytest
from mcp import Client

from flowpilot.config import Settings
from flowpilot.garageflow import GarageFlowClient
from flowpilot.mcp_server import create_server
from flowpilot.sandbox import SandboxState, create_app

# 12:00 in Hyderabad. Seed data and "now" are pinned so every run sees the same data.
NOW = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
BASE_URL = "http://garageflow.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sandbox():
    return create_app(now=NOW)


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


async def _mcp(client: GarageFlowClient) -> AsyncIterator[Client]:
    server = create_server(client=client)
    server.runtime.now = lambda: NOW
    async with Client(server) as mcp_client:
        yield mcp_client


@pytest.fixture
async def mcp(gf) -> AsyncIterator[Client]:
    async for c in _mcp(gf):
        yield c


@pytest.fixture
async def admin_mcp(admin_gf) -> AsyncIterator[Client]:
    async for c in _mcp(admin_gf):
        yield c
