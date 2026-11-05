"""The MCP server, tested the way an AI client uses it: list the tools, call them by name."""

from __future__ import annotations

import pytest

from flowpilot.sandbox import sid

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "check_garageflow_health",
    "list_appointments",
    "get_appointment",
    "get_appointment_history",
    "find_customers",
    "get_customer",
    "get_shop_catalog",
    "check_availability",
    "list_waitlist",
    "search_audit_log",
}


async def call(mcp, name, **args):
    result = await mcp.call_tool(name, args)
    return result


async def ok(mcp, name, **args):
    result = await call(mcp, name, **args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def error_text(mcp, name, **args) -> str:
    result = await call(mcp, name, **args)
    assert result.is_error
    return result.content[0].text


async def test_every_tool_is_listed_with_a_description_and_is_read_only(mcp):
    tools = (await mcp.list_tools()).tools
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description and len(tool.description) > 30, tool.name
        assert tool.annotations and tool.annotations.read_only_hint is True, tool.name


async def test_tool_arguments_have_descriptions_in_the_schema(mcp):
    tools = {t.name: t for t in (await mcp.list_tools()).tools}
    props = tools["check_availability"].input_schema["properties"]
    assert "Oil Change" in props["service"]["description"]
    assert tools["check_availability"].input_schema["required"] == ["service"]


async def test_health(mcp, sandbox_state):
    healthy = await ok(mcp, "check_garageflow_health")
    assert healthy["api"] == "ok" and healthy["database"] == "ok"
    assert healthy["signed_in_as_role"] == "Advisor"

    sandbox_state.faults.database_down = True
    assert (await ok(mcp, "check_garageflow_health"))["database"].startswith("failing")


async def test_list_appointments_resolves_names_and_local_times(mcp):
    today = await ok(mcp, "list_appointments", date="2026-11-04")
    assert today["total_count"] == 5
    first = next(a for a in today["items"] if a["id"] == sid("appointment:t1"))
    assert first["customer"] == "Priya Sharma"
    assert first["technician"] == "Meena Iyer"
    assert first["vehicle"] == "Hyundai Creta (TS08FG5678)"
    assert first["start_local"] == "2026-11-04 09:30 IST"
    assert first["start_utc"] == "2026-11-04T04:00:00Z"


async def test_stale_booking_is_flagged(mcp):
    booked = await ok(mcp, "list_appointments", date="today", status="Booked")
    stale = [a for a in booked["items"] if a["id"] == sid("appointment:stale")]
    assert stale and "background job may not be running" in stale[0]["flags"][0]
    on_time = [a for a in booked["items"] if a["id"] == sid("appointment:t3")]
    assert on_time[0]["flags"] == []


async def test_list_appointments_over_several_days(mcp):
    result = await ok(mcp, "list_appointments", date="yesterday", days=3)
    assert result["total_count"] == 10


async def test_invalid_status_is_rejected_by_the_schema(mcp):
    assert "Booked" in await error_text(mcp, "list_appointments", status="Sleeping")


async def test_get_appointment_and_its_history(mcp):
    appt = await ok(mcp, "get_appointment", appointment_id=sid("appointment:t1"))
    assert appt["inspection_items"][0]["condition"] == "Critical"
    assert any("Critical" in f for f in appt["flags"])

    history = (await ok(mcp, "get_appointment_history", appointment_id=sid("appointment:t1")))["result"]
    assert [h["details"] for h in history][0] == "Booked"
    assert history[-1]["details"] == "Status InInspection -> Completed"


async def test_unknown_appointment_explains_what_to_do(mcp):
    text = await error_text(mcp, "get_appointment", appointment_id=sid("appointment:missing"))
    assert "HTTP 404" in text and "correlationId" in text and "find_customers" in text


async def test_malformed_id_is_caught_before_calling_garageflow(mcp, sandbox_state):
    before = len(sandbox_state.requests_seen)
    assert "not a valid id" in await error_text(mcp, "get_appointment", appointment_id="12345")
    assert len(sandbox_state.requests_seen) == before


async def test_find_customers_by_name(mcp):
    result = await ok(mcp, "find_customers", query="sharma")
    assert result["matched_by"] == "name or email"
    assert [c["name"] for c in result["items"]] == ["Priya Sharma"]


async def test_find_customers_by_registration_number_with_spaces(mcp):
    result = await ok(mcp, "find_customers", query="ts07 hj 9012")
    assert result["matched_by"] == "registration number"
    assert result["items"][0]["name"] == "Sanjay Reddy"
    assert "Product feedback" in result["note"]


async def test_find_customers_with_no_match(mcp):
    result = await ok(mcp, "find_customers", query="nobody-here")
    assert result["total_count"] == 0 and result["items"] == []


async def test_catalog_lists_who_can_do_what(mcp):
    catalog = await ok(mcp, "get_shop_catalog")
    brakes = next(s for s in catalog["service_types"] if s["name"] == "Brake Inspection")
    assert brakes["qualified_technicians"] == ["Arjun Rao"]
    assert catalog["bays"] == ["Bay 1", "Bay 2", "Bay 3"]
    assert catalog["flags"] == []


async def test_catalog_flags_a_service_nobody_can_do(mcp, sandbox_state):
    for tech in sandbox_state.data.technicians.values():
        tech["skills"] = [s for s in tech["skills"] if s != "Tires"]
    catalog = await ok(mcp, "get_shop_catalog")
    assert any("Tyre Rotation" in f for f in catalog["flags"])


async def test_availability_by_partial_name_and_technician(mcp):
    result = await ok(mcp, "check_availability", service="brake", date="tomorrow", technician="arjun")
    assert result["service"] == "Brake Inspection"
    assert result["total_free_slots"] > 0
    assert all(s["technicians"] == ["Arjun Rao"] for s in result["slots"])
    assert not any(s["start_local"].endswith("10:00 IST") for s in result["slots"])


async def test_availability_with_unknown_or_ambiguous_service(mcp):
    assert "Known: Brake Inspection" in await error_text(mcp, "check_availability", service="car wash")
    assert "matches several" in await error_text(mcp, "check_availability", service="o")


async def test_waitlist_flags_an_expired_offer(mcp):
    entries = (await ok(mcp, "list_waitlist"))["result"]
    offered = next(e for e in entries if e["status"] == "Offered")
    assert "background job may not be running" in offered["flags"][0]
    waiting = (await ok(mcp, "list_waitlist", status="Waiting"))["result"]
    assert [e["status"] for e in waiting] == ["Waiting"]


async def test_audit_log_is_refused_for_an_advisor(mcp):
    text = await error_text(mcp, "search_audit_log")
    assert "HTTP 403" in text and "Do not retry" in text


async def test_audit_log_works_for_an_admin(admin_mcp):
    result = await ok(admin_mcp, "search_audit_log", performed_by="system")
    assert result["total_count"] == 1  # the one NoShow marked by the background job
    assert result["items"][0]["details"] == "Status Booked -> NoShow"


async def test_outage_reaches_the_model_with_a_correlation_id(mcp, sandbox_state):
    await ok(mcp, "get_shop_catalog")  # sign in and warm up
    sandbox_state.faults.fail_next = 50
    text = await error_text(mcp, "get_customer", customer_id=sid("customer:ravi"))
    assert "HTTP 503" in text and "correlationId fp-" in text and "check_garageflow_health" in text


async def test_triage_prompt(mcp):
    prompt = await mcp.get_prompt("triage_ticket", {"ticket": "Branch 3 says bookings are stuck"})
    text = prompt.messages[0].content.text
    assert "Branch 3 says bookings are stuck" in text
    assert "needs human approval" in text
