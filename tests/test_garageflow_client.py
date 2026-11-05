"""The integration layer: auth, retries, correlation IDs and error mapping."""

from __future__ import annotations

from datetime import date, datetime

import httpx2 as httpx
import pytest

from flowpilot.garageflow import (
    GarageFlowAuthError,
    GarageFlowClient,
    GarageFlowNotFound,
    GarageFlowRequestError,
    GarageFlowUnavailable,
)
from flowpilot.garageflow.models import AppointmentStatus
from flowpilot.sandbox import sid
from tests.conftest import NOW, make_settings

pytestmark = pytest.mark.anyio


def logins(state) -> int:
    return sum(1 for method, path, _ in state.requests_seen if path == "/api/auth/login")


async def test_signs_in_once_and_reuses_the_token(gf, sandbox_state):
    await gf.list_service_types()
    await gf.list_technicians()
    await gf.list_bays()
    assert logins(sandbox_state) == 1
    assert gf.role == "Advisor"


async def test_signs_in_again_when_the_token_is_about_to_expire(gf, sandbox_state):
    from datetime import timedelta

    sandbox_state.token_lifetime = timedelta(seconds=30)  # inside the 60 s refresh margin
    await gf.list_service_types()
    await gf.list_service_types()
    assert logins(sandbox_state) == 2


async def test_a_revoked_token_is_replaced_once(gf, sandbox_state):
    await gf.list_bays()
    sandbox_state.revoke_all_tokens()  # e.g. GarageFlow restarted with a new signing key
    bays = await gf.list_bays()
    assert len(bays) == 3
    assert logins(sandbox_state) == 2


async def test_wrong_password_is_an_auth_error(sandbox):
    settings = make_settings(password="wrong")
    async with GarageFlowClient(settings, transport=httpx.ASGITransport(app=sandbox)) as client:
        with pytest.raises(GarageFlowAuthError) as err:
            await client.list_bays()
    assert err.value.status == 401


async def test_get_is_retried_on_503_with_the_same_correlation_id(gf, sandbox_state, sleeps):
    await gf.list_bays()  # sign in first so only the data call is counted
    sandbox_state.faults.fail_next = 2
    bays = await gf.list_bays()

    assert len(bays) == 3
    assert len(sleeps.calls) == 2
    assert sleeps.calls[1] > sleeps.calls[0]  # exponential backoff
    ids = [cid for method, path, cid in sandbox_state.requests_seen if path == "/api/bays"]
    assert len(ids) == 4  # first call + 2 failures + success
    assert len(set(ids[1:])) == 1 and ids[1].startswith("fp-")  # one ID for every attempt of one call


async def test_retry_after_header_is_honoured(gf, sandbox_state, sleeps):
    await gf.list_bays()
    sandbox_state.faults.fail_next = 1
    sandbox_state.faults.fail_status = 429
    sandbox_state.faults.retry_after = 3
    await gf.list_bays()
    assert sleeps.calls == [3.0]


async def test_gives_up_after_max_retries(gf, sandbox_state, sleeps):
    await gf.list_bays()
    sandbox_state.faults.fail_next = 10
    with pytest.raises(GarageFlowUnavailable) as err:
        await gf.list_bays()
    assert len(sleeps.calls) == 3  # max_retries
    assert err.value.status == 503
    assert err.value.correlation_id.startswith("fp-")
    assert "correlationId fp-" in err.value.for_humans()


async def test_forbidden_is_not_retried(gf, sleeps):
    with pytest.raises(GarageFlowAuthError) as err:
        await gf.list_audit()
    assert err.value.status == 403
    assert sleeps.calls == []


async def test_not_found_keeps_garageflows_message(gf):
    with pytest.raises(GarageFlowNotFound) as err:
        await gf.get_appointment(sid("appointment:does-not-exist"))
    assert "was not found" in str(err.value)
    assert err.value.correlation_id.startswith("fp-")


async def test_bad_input_is_a_request_error(gf):
    with pytest.raises(GarageFlowRequestError) as err:
        await gf.list_customers(page_size=500)
    assert err.value.status == 400


async def test_network_failure_becomes_unavailable(sleeps):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with GarageFlowClient(make_settings(retries=2), transport=httpx.MockTransport(refuse), sleep=sleeps) as client:
        with pytest.raises(GarageFlowUnavailable) as err:
            await client.list_bays()
    assert "Could not reach GarageFlow" in str(err.value)


async def test_health_reports_a_dead_database_without_raising_or_retrying(gf, sandbox_state, sleeps):
    sandbox_state.faults.database_down = True
    status = await gf.health()
    assert status["api"] == "ok"
    assert status["database"].startswith("failing")
    assert sleeps.calls == []


async def test_appointments_are_typed_and_filtered(gf):
    page = await gf.list_appointments(status=AppointmentStatus.BOOKED, page_size=100)
    assert page.total_count == 5
    assert all(a.status is AppointmentStatus.BOOKED for a in page.items)
    assert page.items == sorted(page.items, key=lambda a: a.start_time_utc)


async def test_naive_datetimes_are_refused(gf):
    with pytest.raises(ValueError):
        await gf.list_appointments(from_utc=datetime(2026, 11, 4))


async def test_availability_skips_busy_slots(gf):
    brakes = sid("service:brakes")
    tomorrow = await gf.get_availability(service_type_id=brakes, on=date(2026, 11, 5))
    starts = [s.start_local.strftime("%H:%M") for s in tomorrow.slots]
    assert "09:00" in starts
    assert "10:00" not in starts  # Arjun (the only brakes technician) is booked 10:00-11:00
    assert tomorrow.time_zone_id == "Asia/Kolkata"


def test_now_is_pinned():
    assert NOW.tzinfo is not None
