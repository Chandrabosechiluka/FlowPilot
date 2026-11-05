"""FlowPilot's MCP server: GarageFlow, exposed as tools an AI agent can call.

MCP (Model Context Protocol) is a standard way for an AI client (Claude Desktop, Claude Code, Cursor,
or FlowPilot's own Week 3 agent) to discover and call tools. The client asks "what tools do you
have?", gets a JSON Schema for each one, and calls them by name. This file is the adapter between
that world and GarageFlow's REST API.

Design rules, all of which you should be able to defend in an interview:

1. **Read-only in Week 1.** Every tool is marked `readOnlyHint`. Write tools (book, cancel) arrive in
   Week 3 behind a human-approval step, because an agent must never change a customer's data on
   its own.
2. **Least privilege.** FlowPilot signs in as an Advisor, not an Admin. Tools that need Admin
   (the audit log) say so clearly when they are refused, instead of FlowPilot holding a stronger
   account "just in case".
3. **Errors the model can act on.** A failure becomes a `ToolError` with what happened, the
   correlation ID, and what to try next. A crash would show the model only "Error executing tool".
4. **Few, task-shaped tools.** Not one tool per endpoint: tools match questions a support engineer
   asks ("what is going on with this appointment?"), and they return names, local times and flags.
"""

from __future__ import annotations

import functools
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, ParamSpec, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from flowpilot.config import KnowledgeSettings, Settings, load_knowledge_settings, load_settings
from flowpilot.garageflow import (
    GarageFlowAuthError,
    GarageFlowClient,
    GarageFlowError,
    GarageFlowNotFound,
    GarageFlowRequestError,
    GarageFlowUnavailable,
)
from flowpilot.garageflow.models import AppointmentStatus, Customer, WaitlistStatus
from flowpilot.knowledge import KnowledgeBase
from flowpilot.logs import LogStore
from flowpilot.mcp_server import views
from flowpilot.mcp_server.views import Catalog

log = logging.getLogger("flowpilot.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CATALOG_TTL_SECONDS = 300
REGISTRATION_SCAN_MAX_PAGES = 10  # 10 x 100 customers; beyond that, ask the customer to add the search to GarageFlow
REGISTRATION_PATTERN = re.compile(r"^[A-Za-z]{2}\s?\d{1,2}\s?[A-Za-z]{0,3}\s?\d{1,4}$")  # e.g. TS09EX1234

INSTRUCTIONS = """\
FlowPilot gives read-only access to one auto-service shop's GarageFlow system (appointments,
customers, technicians, bays, availability, waitlist and audit trail).
- Start with check_garageflow_health when something "is not working".
- Times are shown in the shop's local time zone; *_utc fields are UTC.
- `flags` on a record are rule-based warnings computed by FlowPilot: treat them as evidence.
- Every error includes a correlationId: quote it when reporting a problem to engineers.
- search_knowledge finds runbooks and past tickets: cite their `citation` when you rely on them.
- search_logs / summarize_log_errors read GarageFlow's API logs (by correlation ID, level, text, time).
- These tools cannot change anything. If a fix needs a change, describe it for a human to approve."""

P = ParamSpec("P")
R = TypeVar("R")


class _Runtime:
    """What the tools share: the GarageFlow client, settings, and a short-lived catalog cache."""

    def __init__(self, settings: Settings, client: GarageFlowClient | None) -> None:
        self.settings = settings
        self.tz = ZoneInfo(settings.shop_timezone)
        self._client = client
        self.knowledge: KnowledgeBase | None = None
        self.logs: LogStore | None = None
        self._catalog: Catalog | None = None
        self._catalog_loaded_at = 0.0
        self.now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    @property
    def gf(self) -> GarageFlowClient:
        if self._client is None:
            raise ToolError("FlowPilot is not connected to GarageFlow yet (server still starting).")
        return self._client

    def attach(self, client: GarageFlowClient | None) -> None:
        self._client = client

    async def catalog(self, *, refresh: bool = False) -> Catalog:
        if refresh or self._catalog is None or time.monotonic() - self._catalog_loaded_at > CATALOG_TTL_SECONDS:
            services = await self.gf.list_service_types()
            techs = await self.gf.list_technicians()
            bays = await self.gf.list_bays()
            self._catalog = Catalog(
                service_types={s.id: s for s in services},
                technicians={t.id: t for t in techs},
                bays={b.id: b for b in bays},
            )
            self._catalog_loaded_at = time.monotonic()
        return self._catalog

    async def customers(self, ids: set[UUID]) -> dict[UUID, Customer]:
        """Look customers up for name resolution. A missing customer is shown by id, not an error."""
        found: dict[UUID, Customer] = {}
        for cid in ids:
            try:
                found[cid] = await self.gf.get_customer(cid)
            except GarageFlowNotFound:
                continue
        return found


def _hint(exc: GarageFlowError) -> str:
    if isinstance(exc, GarageFlowAuthError):
        if exc.status == 403:
            return ("FlowPilot's GarageFlow account does not have the role this needs. "
                    "Do not retry; tell a human which role is required.")
        return "FlowPilot's GarageFlow sign-in failed. Check GARAGEFLOW_EMAIL / GARAGEFLOW_PASSWORD."
    if isinstance(exc, GarageFlowNotFound):
        return "That id does not exist. Use find_customers or list_appointments to look up the right one."
    if isinstance(exc, GarageFlowUnavailable):
        return "GarageFlow is down or overloaded (retries already done). Run check_garageflow_health."
    if isinstance(exc, GarageFlowRequestError):
        return "GarageFlow rejected the request; fix the arguments using the message above."
    return "Unexpected GarageFlow error."


def tool_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Turn GarageFlow errors into ToolErrors the model can read and act on."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        except GarageFlowError as exc:
            log.info("tool %s failed: %s", fn.__name__, exc.for_humans())
            raise ToolError(f"{exc.for_humans()}. {_hint(exc)}") from exc
        finally:
            log.info("tool %s took %.0f ms", fn.__name__, (time.perf_counter() - started) * 1000)

    return wrapper


def _parse_day(value: str | None, tz: ZoneInfo, now: datetime) -> date:
    if not value or value.lower() == "today":
        return now.astimezone(tz).date()
    if value.lower() == "tomorrow":
        return now.astimezone(tz).date() + timedelta(days=1)
    if value.lower() == "yesterday":
        return now.astimezone(tz).date() - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"date {value!r} must be YYYY-MM-DD, 'today', 'tomorrow' or 'yesterday'.") from exc


def _parse_id(value: str, what: str) -> UUID:
    try:
        return UUID(value.strip())
    except ValueError as exc:
        raise ToolError(f"{what} {value!r} is not a valid id (expected a GUID like 3f2a...-...).") from exc


def _normalise_registration(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


def create_server(
    *,
    settings: Settings | None = None,
    client: GarageFlowClient | None = None,
    knowledge: KnowledgeBase | None = None,
    logs: LogStore | None = None,
    knowledge_settings: KnowledgeSettings | None = None,
) -> MCPServer:
    """Build the server.

    - Tests pass an already-open `client` (pointed at the sandbox), an ingested `knowledge` base and a
      `logs` store, and own their lifetimes.
    - In production nothing is passed: settings come from the environment, and the lifespan opens the
      client, builds and ingests the knowledge base, and opens the log file when the server starts.
    """
    runtime = _Runtime(settings or (client and client._settings) or load_settings(), client)  # noqa: SLF001
    runtime.knowledge, runtime.logs = knowledge, logs
    testing = client is not None

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        if testing:
            yield
            return
        from pathlib import Path

        from flowpilot.knowledge.factory import build_knowledge_base

        ks = knowledge_settings or load_knowledge_settings()
        kb: KnowledgeBase | None = None
        try:
            kb = build_knowledge_base(ks)
            report = await kb.ingest(Path(ks.knowledge_dir))
            log.info("Knowledge base ready: %s", report.summary())
            runtime.knowledge = kb
        except Exception as exc:  # noqa: BLE001 - the GarageFlow tools still work without it
            log.error("Knowledge base not available: %s", exc)
        if ks.log_file:
            runtime.logs = LogStore(Path(ks.log_file))
            log.info("Reading GarageFlow logs from %s", ks.log_file)
        async with GarageFlowClient(runtime.settings) as gf:
            runtime.attach(gf)
            try:
                yield
            finally:
                runtime.attach(None)
                if kb is not None:
                    await kb.store.close()

    server = MCPServer(
        name="flowpilot-garageflow",
        title="FlowPilot for GarageFlow",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        lifespan=lifespan,
    )
    server.runtime = runtime  # type: ignore[attr-defined]  # handy in tests

    # ------------------------------------------------------------ tools

    @server.tool(title="Check GarageFlow health", annotations=READ_ONLY)
    @tool_errors
    async def check_garageflow_health() -> views.HealthView:
        """Is the customer's GarageFlow API up, is its database reachable, and can FlowPilot sign in?
        Run this first when someone reports that something is not working."""
        status = await runtime.gf.health()
        role, note = None, None
        if status.get("api") == "ok":
            try:
                await runtime.gf.me()
                role = runtime.gf.role.value if runtime.gf.role else None
            except GarageFlowError as exc:
                note = f"API is up but FlowPilot cannot sign in: {exc.for_humans()}"
        return views.HealthView(
            garageflow_url=runtime.settings.garageflow_base_url,
            api=status.get("api", "unknown"),
            database=status.get("database", "unknown"),
            signed_in_as_role=role,
            note=note,
        )

    @server.tool(title="List appointments", annotations=READ_ONLY)
    @tool_errors
    async def list_appointments(
        date: Annotated[str | None, Field(description="Shop-local day: YYYY-MM-DD, 'today', 'tomorrow' or 'yesterday'. Default today.")] = None,
        days: Annotated[int, Field(ge=1, le=14, description="How many days from `date` to include (1-14).")] = 1,
        status: Annotated[AppointmentStatus | None, Field(description="Only appointments in this status.")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> views.AppointmentList:
        """Appointments for one or more days in the shop's local time, with customer, vehicle, service,
        technician and bay names, plus rule-based `flags` for records that look wrong."""
        now = runtime.now()
        day = _parse_day(date, runtime.tz, now)
        start = datetime.combine(day, datetime.min.time(), runtime.tz)
        result = await runtime.gf.list_appointments(
            from_utc=start, to_utc=start + timedelta(days=days), status=status, page=page, page_size=20
        )
        catalog = await runtime.catalog()
        customers = await runtime.customers({a.customer_id for a in result.items})
        return views.AppointmentList(
            total_count=result.total_count,
            page=result.page,
            has_next=result.has_next,
            time_zone=runtime.settings.shop_timezone,
            items=[views.appointment_view(a, catalog, customers.get(a.customer_id), runtime.tz, now) for a in result.items],
        )

    @server.tool(title="Get appointment", annotations=READ_ONLY)
    @tool_errors
    async def get_appointment(
        appointment_id: Annotated[str, Field(description="The appointment id (GUID).")],
    ) -> views.AppointmentView:
        """Everything about one appointment: status, people, vehicle, times, inspection items and flags."""
        a = await runtime.gf.get_appointment(_parse_id(appointment_id, "appointment_id"))
        catalog = await runtime.catalog()
        customers = await runtime.customers({a.customer_id})
        return views.appointment_view(a, catalog, customers.get(a.customer_id), runtime.tz, runtime.now())

    @server.tool(title="Appointment history", annotations=READ_ONLY)
    @tool_errors
    async def get_appointment_history(
        appointment_id: Annotated[str, Field(description="The appointment id (GUID).")],
    ) -> list[views.AuditEntryView]:
        """Who changed this appointment and when, oldest first (booked, checked in, cancelled...).
        Use it to answer "why is this appointment in this state?"."""
        entries = await runtime.gf.get_appointment_history(_parse_id(appointment_id, "appointment_id"))
        return [views.audit_view(e, runtime.tz) for e in entries]

    @server.tool(title="Find customers", annotations=READ_ONLY)
    @tool_errors
    async def find_customers(
        query: Annotated[str, Field(min_length=2, description="Part of a name or email, or a vehicle registration number like TS09EX1234.")],
    ) -> views.CustomerSearchResult:
        """Find customers and their vehicles by name, email or vehicle registration number."""
        query = query.strip()
        if REGISTRATION_PATTERN.match(query):
            wanted = _normalise_registration(query)
            matches: list[Customer] = []
            page = 1
            while page <= REGISTRATION_SCAN_MAX_PAGES:
                result = await runtime.gf.list_customers(page=page, page_size=100)
                matches += [c for c in result.items
                            if any(_normalise_registration(v.registration_number) == wanted for v in c.vehicles)]
                if not result.has_next:
                    break
                page += 1
            note = ("GarageFlow cannot search by registration number, so FlowPilot scanned the customer list. "
                    "Product feedback: add registration to GarageFlow's customer search.")
            if page > REGISTRATION_SCAN_MAX_PAGES:
                note += f" Only the first {REGISTRATION_SCAN_MAX_PAGES * 100} customers were scanned."
            return views.CustomerSearchResult(
                matched_by="registration number", total_count=len(matches),
                items=[views.customer_view(c) for c in matches], note=note,
            )
        result = await runtime.gf.list_customers(search=query, page_size=20)
        return views.CustomerSearchResult(
            matched_by="name or email",
            total_count=result.total_count,
            items=[views.customer_view(c) for c in result.items],
            note="Showing the first 20 matches." if result.has_next else None,
        )

    @server.tool(title="Get customer", annotations=READ_ONLY)
    @tool_errors
    async def get_customer(
        customer_id: Annotated[str, Field(description="The customer id (GUID).")],
    ) -> views.CustomerView:
        """One customer's contact details and vehicles."""
        return views.customer_view(await runtime.gf.get_customer(_parse_id(customer_id, "customer_id")))

    @server.tool(title="Shop catalog", annotations=READ_ONLY)
    @tool_errors
    async def get_shop_catalog() -> views.CatalogView:
        """The shop's service types (with who is qualified to do them), technicians and bays.
        Flags point out set-up mistakes, such as a service nobody is qualified for."""
        return views.catalog_view(await runtime.catalog(refresh=True))

    @server.tool(title="Check availability", annotations=READ_ONLY)
    @tool_errors
    async def check_availability(
        service: Annotated[str, Field(description="Service type name (e.g. 'Oil Change') or id.")],
        date: Annotated[str | None, Field(description="Shop-local day: YYYY-MM-DD, 'today' or 'tomorrow'. Default today.")] = None,
        technician: Annotated[str | None, Field(description="Optional technician name or id.")] = None,
    ) -> views.AvailabilityView:
        """Free start times for a service on one day, with which technicians and bays are free.
        Read-only: it does not hold or book a slot."""
        catalog = await runtime.catalog()
        service_type = _match(service, catalog.service_types.values(), "service")
        tech = _match(technician, catalog.technicians.values(), "technician") if technician else None
        day = _parse_day(date, runtime.tz, runtime.now())
        result = await runtime.gf.get_availability(
            service_type_id=service_type.id, on=day, technician_id=tech.id if tech else None
        )
        slots = [
            views.SlotView(
                start_local=views.to_local(s.start_utc, runtime.tz),
                start_utc=views.to_utc(s.start_utc),
                technicians=[catalog.technician_name(t) for t in s.available_technician_ids],
                bays=[catalog.bay_name(b) for b in s.available_bay_ids],
            )
            for s in result.slots
        ]
        return views.AvailabilityView(
            date=result.date.isoformat(), time_zone=result.time_zone_id, service=service_type.name,
            duration_minutes=result.duration_minutes, total_free_slots=len(slots), slots=slots[:12],
        )

    @server.tool(title="Waitlist", annotations=READ_ONLY)
    @tool_errors
    async def list_waitlist(
        status: Annotated[WaitlistStatus | None, Field(description="Only entries in this status.")] = None,
    ) -> list[views.WaitlistEntryView]:
        """Customers waiting for a slot to open, and offers made to them, with flags for stuck offers."""
        entries = await runtime.gf.list_waitlist(status=status)
        catalog = await runtime.catalog()
        now = runtime.now()
        return [views.waitlist_view(w, catalog, runtime.tz, now) for w in entries]

    @server.tool(title="Search audit log", annotations=READ_ONLY)
    @tool_errors
    async def search_audit_log(
        entity_name: Annotated[str | None, Field(description="e.g. Appointment, Customer, WaitlistEntry, AppUser")] = None,
        entity_id: Annotated[str | None, Field(description="Id of one record")] = None,
        performed_by: Annotated[str | None, Field(description="User email, or 'system' for background jobs")] = None,
        page: Annotated[int, Field(ge=1)] = 1,
    ) -> views.AuditPage:
        """The shop-wide audit trail, newest first. Needs an Admin account in GarageFlow; with FlowPilot's
        default Advisor account it is refused (use get_appointment_history for a single appointment)."""
        result = await runtime.gf.list_audit(
            entity_name=entity_name,
            entity_id=_parse_id(entity_id, "entity_id") if entity_id else None,
            performed_by=performed_by,
            page=page,
        )
        return views.AuditPage(
            total_count=result.total_count, page=result.page, has_next=result.has_next,
            items=[views.audit_view(e, runtime.tz) for e in result.items],
        )

    from flowpilot.mcp_server.knowledge_tools import register_knowledge_tools

    register_knowledge_tools(server, runtime)

    # ---------------------------------------------------------- prompts

    @server.prompt(title="Triage a support ticket")
    def triage_ticket(ticket: str) -> str:
        """A starting prompt for investigating a customer's support ticket with these tools."""
        return (
            "You are a support engineer for an auto-service shop that runs GarageFlow. Investigate this ticket "
            "using only the FlowPilot tools, which are read-only.\n\n"
            f"Ticket:\n{ticket}\n\n"
            "Steps: 1) check_garageflow_health. 2) Find the records involved (find_customers, list_appointments, "
            "get_appointment). 3) Read their history and flags. 4) Check the logs (summarize_log_errors, then "
            "search_logs). 5) Search runbooks and past tickets (search_knowledge) for the symptom and the errors. "
            "6) Answer with: what is happening, the evidence (tool results, with ids, correlationIds and citations), "
            "the likely root cause, and the fix you recommend. Mark anything that needs a change to data as "
            "'needs human approval'. If the evidence is not enough, say what a human must check."
        )

    return server


def _match(value: str, items: Any, what: str) -> Any:
    """Find a catalog item by id or by name (case-insensitive, exact or unique partial match)."""
    items = list(items)
    try:
        wanted = UUID(value)
        for item in items:
            if item.id == wanted:
                return item
    except ValueError:
        pass
    lowered = value.strip().lower()
    exact = [i for i in items if i.name.lower() == lowered]
    if exact:
        return exact[0]
    partial = [i for i in items if lowered in i.name.lower()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(sorted(i.name for i in items))
    if partial:
        raise ToolError(f"{what} {value!r} matches several: {', '.join(i.name for i in partial)}. Be more specific.")
    raise ToolError(f"No {what} called {value!r}. Known: {names}.")
