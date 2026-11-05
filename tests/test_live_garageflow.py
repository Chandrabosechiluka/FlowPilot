"""Contract check against a REAL GarageFlow (the .NET API), skipped unless you ask for it.

The sandbox is a copy of GarageFlow's contract, and copies drift. Run this whenever GarageFlow
changes, with the .NET API running in Development (demo users and data):

    PowerShell:  $env:GARAGEFLOW_LIVE="1"; uv run pytest tests/test_live_garageflow.py -v
    bash:        GARAGEFLOW_LIVE=1 uv run pytest tests/test_live_garageflow.py -v

It only reads. If a model fails to parse here, the real API changed shape: update models.py and
the sandbox together.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from flowpilot.config import load_settings
from flowpilot.garageflow import GarageFlowAuthError, GarageFlowClient

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(os.environ.get("GARAGEFLOW_LIVE") != "1", reason="set GARAGEFLOW_LIVE=1 to run against a real GarageFlow"),
]


@pytest.fixture
async def live():
    async with GarageFlowClient(load_settings()) as client:
        yield client


async def test_health(live):
    assert await live.health() == {"api": "ok", "database": "ok"}


async def test_every_read_endpoint_parses(live):
    services = await live.list_service_types()
    await live.list_technicians()
    await live.list_bays()
    await live.list_waitlist()
    customers = await live.list_customers(page_size=5)
    if customers.items:
        await live.get_customer(customers.items[0].id)
    now = datetime.now(timezone.utc)
    page = await live.list_appointments(from_utc=now - timedelta(days=30), to_utc=now + timedelta(days=30), page_size=5)
    if page.items:
        await live.get_appointment(page.items[0].id)
        await live.get_appointment_history(page.items[0].id)
    if services:
        await live.get_availability(service_type_id=services[0].id, on=(now + timedelta(days=1)).date())


async def test_audit_matches_the_role(live):
    if live.role is None:
        await live.me()
    try:
        await live.list_audit(page_size=1)
        assert live.role == "Admin"
    except GarageFlowAuthError as exc:
        assert exc.status == 403 and live.role != "Admin"
