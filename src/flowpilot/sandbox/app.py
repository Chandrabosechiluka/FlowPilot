"""A fake GarageFlow: the same routes, JSON shapes, roles and error bodies as the real .NET API.

Why it exists:
- **Tests** run in milliseconds with no SQL Server, no .NET and no network (CI stays simple).
- **Demos** work on any laptop: `flowpilot-sandbox` starts it on port 5090.
- **Fault injection**: a Forward Deployed Engineer spends real time on "it works on my machine but
  not at the customer's site". The sandbox can be told to fail (503s, a dead database, revoked
  tokens, slow responses) so FlowPilot's retry and error handling are proven, and so the Week 3
  agent has realistic incidents to diagnose.

It is deliberately small. The real GarageFlow stays the source of truth: when the two disagree,
fix the sandbox. Contract: src/GarageFlow.Application/Contracts/*.cs and the controllers.

Starlette is the framework FastAPI is built on, so this file is also a first look at the
request/response style FastAPI uses.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from flowpilot.sandbox.seed import SandboxData, build_seed

PASSWORD = "Garage@12345"  # same as GarageFlow's DbSeeder.DevPassword
ADMIN_OR_ADVISOR = {"Admin", "Advisor"}


@dataclass
class Faults:
    """Switch failures on at runtime (tests set these directly; demos use POST /_sandbox/faults)."""

    fail_next: int = 0  # the next N /api requests answer `fail_status`
    fail_status: int = 503
    retry_after: int | None = None
    database_down: bool = False  # /health/db answers 503
    delay_seconds: float = 0.0  # every /api request waits this long first


@dataclass
class SandboxState:
    data: SandboxData
    faults: Faults = field(default_factory=Faults)
    tokens: dict[str, tuple[str, datetime]] = field(default_factory=dict)  # token -> (email, expires)
    token_lifetime: timedelta = timedelta(hours=1)
    requests_seen: list[tuple[str, str, str | None]] = field(default_factory=list)  # (method, path, correlationId)

    def revoke_all_tokens(self) -> None:
        self.tokens.clear()


# ----------------------------------------------------------------- helpers


def _correlation_id(request: Request) -> str:
    supplied = request.headers.get("X-Correlation-ID", "")
    ok = 0 < len(supplied) <= 64 and all(c.isalnum() or c in "-_" for c in supplied)
    return supplied if ok else uuid4().hex


def _json(request: Request, body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers={"X-Correlation-ID": request.state.correlation_id})


def _problem(request: Request, status: int, title: str, detail: str | None = None) -> JSONResponse:
    body = {"status": status, "title": title, "detail": detail, "correlationId": request.state.correlation_id}
    response = JSONResponse(body, status_code=status, media_type="application/problem+json")
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    return response


def _state(request: Request) -> SandboxState:
    return request.app.state.sandbox


def _user(request: Request) -> dict[str, Any] | None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    entry = _state(request).tokens.get(auth.removeprefix("Bearer "))
    if entry is None or entry[1] <= datetime.now(timezone.utc):
        return None
    return _state(request).data.users[entry[0]]


def _page_args(request: Request) -> tuple[int, int] | JSONResponse:
    try:
        page = int(request.query_params.get("page", "1"))
        size = int(request.query_params.get("pageSize", "20"))
    except ValueError:
        return _problem(request, 400, "Invalid request", "page and pageSize must be whole numbers.")
    if page < 1:
        return _problem(request, 400, "Invalid request", "page must be 1 or more.")
    if not 1 <= size <= 100:
        return _problem(request, 400, "Invalid request", "pageSize must be between 1 and 100.")
    return page, size


def _paged(items: list[dict[str, Any]], page: int, size: int) -> dict[str, Any]:
    total = len(items)
    total_pages = 0 if total == 0 else -(-total // size)
    return {
        "items": items[(page - 1) * size : page * size],
        "page": page,
        "pageSize": size,
        "totalCount": total,
        "totalPages": total_pages,
        "hasPrevious": page > 1,
        "hasNext": page < total_pages,
    }


def _uuid(request: Request, value: str | None, name: str) -> UUID | JSONResponse | None:
    if value is None or value == "":
        return None
    try:
        return UUID(value)
    except ValueError:
        return _problem(request, 400, "Invalid request", f"{name} is not a valid id.")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ------------------------------------------------------------- middleware


class SandboxMiddleware:
    """Correlation IDs, auth, role checks and fault injection, the way GarageFlow's pipeline does them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        request.state.correlation_id = _correlation_id(request)
        state: SandboxState = scope["app"].state.sandbox
        path = request.url.path
        state.requests_seen.append((request.method, path, request.headers.get("X-Correlation-ID")))

        if path.startswith("/api/"):
            if state.faults.delay_seconds:
                await anyio.sleep(state.faults.delay_seconds)
            if state.faults.fail_next > 0:
                state.faults.fail_next -= 1
                response = _problem(request, state.faults.fail_status, "Service unavailable", "Injected by the sandbox.")
                if state.faults.retry_after is not None:
                    response.headers["Retry-After"] = str(state.faults.retry_after)
                await response(scope, receive, send)
                return
            if path != "/api/auth/login" and _user(request) is None:
                await _problem(request, 401, "Unauthorized")(scope, receive, send)
                return

        await self.app(scope, receive, send)


def requires(roles: set[str]):
    def decorator(handler):
        async def wrapped(request: Request) -> Response:
            user = _user(request)
            if user is None:
                return _problem(request, 401, "Unauthorized")
            if user["role"] not in roles:
                return _problem(request, 403, "Forbidden", f"This needs one of the roles: {', '.join(sorted(roles))}.")
            return await handler(request)

        return wrapped

    return decorator


# ----------------------------------------------------------------- routes


async def health(request: Request) -> Response:
    return _json(request, {"status": "Healthy"})


async def health_db(request: Request) -> Response:
    if _state(request).faults.database_down:
        return _problem(request, 503, "Service Unavailable", "The database is not reachable.")
    return _json(request, {"database": "Reachable"})


async def login(request: Request) -> Response:
    try:
        body = await request.json()
    except ValueError:
        return _problem(request, 400, "Invalid request", "Body must be JSON.")
    state = _state(request)
    user = state.data.users.get(str(body.get("email", "")).lower())
    if user is None or body.get("password") != PASSWORD or not user["isActive"]:
        return _problem(request, 401, "Sign-in failed", "Email or password is wrong.")
    token = secrets.token_hex(24)
    expires = datetime.now(timezone.utc) + state.token_lifetime
    state.tokens[token] = (user["email"], expires)
    return _json(
        request,
        {
            "accessToken": token,
            "expiresAtUtc": expires.isoformat().replace("+00:00", "Z"),
            "role": user["role"],
            "displayName": user["displayName"],
            "tokenType": "Bearer",
        },
    )


async def me(request: Request) -> Response:
    user = _user(request)
    return _json(request, {"email": user["email"], "displayName": user["displayName"], "role": user["role"]})


async def list_appointments(request: Request) -> Response:
    paging = _page_args(request)
    if isinstance(paging, JSONResponse):
        return paging
    q = request.query_params
    items = sorted(_state(request).data.appointments.values(), key=lambda a: (a["startTimeUtc"], a["id"]))
    if q.get("fromUtc"):
        items = [a for a in items if _parse_utc(a["startTimeUtc"]) >= _parse_utc(q["fromUtc"])]
    if q.get("toUtc"):
        items = [a for a in items if _parse_utc(a["startTimeUtc"]) < _parse_utc(q["toUtc"])]
    if q.get("status"):
        items = [a for a in items if a["status"] == q["status"]]
    return _json(request, _paged(items, *paging))


async def get_appointment(request: Request) -> Response:
    appointment = _state(request).data.appointments.get(request.path_params["id"])
    if appointment is None:
        return _problem(request, 404, "Not found", "Appointment was not found.")
    return _json(request, appointment)


@requires(ADMIN_OR_ADVISOR)
async def appointment_history(request: Request) -> Response:
    appointment_id = request.path_params["id"]
    if appointment_id not in _state(request).data.appointments:
        return _problem(request, 404, "Not found", "Appointment was not found.")
    rows = [a for a in _state(request).data.audit if a["entityId"] == appointment_id]
    return _json(request, sorted(rows, key=lambda a: (a["timestampUtc"], a["id"])))


async def list_customers(request: Request) -> Response:
    paging = _page_args(request)
    if isinstance(paging, JSONResponse):
        return paging
    search = (request.query_params.get("search") or "").strip().lower()
    items = sorted(_state(request).data.customers.values(), key=lambda c: (c["fullName"], c["id"]))
    if search:  # exactly like CustomerService.ListAsync: name or email only
        items = [c for c in items if search in c["fullName"].lower() or search in c["email"].lower()]
    return _json(request, _paged(items, *paging))


async def get_customer(request: Request) -> Response:
    customer = _state(request).data.customers.get(request.path_params["id"])
    if customer is None:
        return _problem(request, 404, "Not found", "Customer was not found.")
    return _json(request, customer)


async def service_types(request: Request) -> Response:
    return _json(request, list(_state(request).data.service_types.values()))


async def technicians(request: Request) -> Response:
    return _json(request, list(_state(request).data.technicians.values()))


async def bays(request: Request) -> Response:
    return _json(request, list(_state(request).data.bays.values()))


async def availability(request: Request) -> Response:
    q = request.query_params
    service_type_id = _uuid(request, q.get("serviceTypeId"), "serviceTypeId")
    technician_id = _uuid(request, q.get("technicianId"), "technicianId")
    for value in (service_type_id, technician_id):
        if isinstance(value, JSONResponse):
            return value
    if service_type_id is None or not q.get("date"):
        return _problem(request, 400, "Invalid request", "serviceTypeId and date are required.")
    data = _state(request).data
    service = data.service_types.get(str(service_type_id))
    if service is None:
        return _problem(request, 404, "Not found", "Service type was not found.")
    try:
        day = datetime.strptime(q["date"], "%Y-%m-%d").date()
    except ValueError:
        return _problem(request, 400, "Invalid request", "date must look like 2026-11-05.")
    return _json(request, data.availability(service, day, str(technician_id) if technician_id else None))


async def waitlist(request: Request) -> Response:
    status = request.query_params.get("status")
    rows = sorted(_state(request).data.waitlist, key=lambda w: w["createdUtc"])
    if status:
        rows = [w for w in rows if w["status"] == status]
    return _json(request, rows[:200])


@requires({"Admin"})
async def audit(request: Request) -> Response:
    paging = _page_args(request)
    if isinstance(paging, JSONResponse):
        return paging
    q = request.query_params
    rows = sorted(_state(request).data.audit, key=lambda a: a["timestampUtc"], reverse=True)
    if q.get("entityName"):
        rows = [a for a in rows if a["entityName"] == q["entityName"]]
    if q.get("entityId"):
        rows = [a for a in rows if a["entityId"] == q["entityId"]]
    if q.get("performedBy"):
        rows = [a for a in rows if a["performedBy"] == q["performedBy"]]
    return _json(request, _paged(rows, *paging))


async def set_faults(request: Request) -> Response:
    """Demo helper (not part of GarageFlow): POST {"fail_next": 2, "database_down": true} to break things."""
    body = await request.json()
    faults = _state(request).faults
    for key, value in body.items():
        if not hasattr(faults, key):
            return _problem(request, 400, "Unknown fault", key)
        setattr(faults, key, value)
    if body.get("revoke_tokens"):
        _state(request).revoke_all_tokens()
    return _json(request, faults.__dict__)


def create_app(*, now: datetime | None = None) -> Starlette:
    """`now` pins the seed data to a fixed moment so tests are repeatable."""
    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/health/db", health_db),
            Route("/api/auth/login", login, methods=["POST"]),
            Route("/api/auth/me", me),
            Route("/api/appointments", list_appointments),
            Route("/api/appointments/{id}", get_appointment),
            Route("/api/appointments/{id}/history", appointment_history),
            Route("/api/customers", list_customers),
            Route("/api/customers/{id}", get_customer),
            Route("/api/service-types", service_types),
            Route("/api/technicians", technicians),
            Route("/api/bays", bays),
            Route("/api/availability", availability),
            Route("/api/waitlist", waitlist),
            Route("/api/audit", audit),
            Route("/_sandbox/faults", set_faults, methods=["POST"]),
        ]
    )
    app.state.sandbox = SandboxState(data=build_seed(now or datetime.now(timezone.utc)))
    app.add_middleware(SandboxMiddleware)
    return app
