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

import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from flowpilot.sandbox.logs import RequestLogger, write_history
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
    request_logger: RequestLogger | None = None

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

        if state.request_logger is not None and path.startswith("/api/"):
            started = time.perf_counter()
            status_holder = {"status": 500}
            logger, inner_send = state.request_logger, send

            async def logging_send(message: Any) -> None:  # sees the status code on its way out
                if message["type"] == "http.response.start":
                    status_holder["status"] = message["status"]
                elif message["type"] == "http.response.body" and not message.get("more_body"):
                    logger.log(
                        request.method, path, status_holder["status"],
                        int((time.perf_counter() - started) * 1000), request.state.correlation_id,
                    )
                await inner_send(message)

            send = logging_send

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
                return _problem(request, 403, "Forbidden")  # GarageFlow's 403 has no detail either
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
    if user is None:
        return _problem(request, 401, "Unauthorized")
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
        return _problem(request, 404, "Resource not found", f"Appointment with id {request.path_params['id']} was not found.")
    return _json(request, appointment)


@requires(ADMIN_OR_ADVISOR)
async def appointment_history(request: Request) -> Response:
    appointment_id = request.path_params["id"]
    if appointment_id not in _state(request).data.appointments:
        return _problem(request, 404, "Resource not found", f"Appointment with id {request.path_params['id']} was not found.")
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
        return _problem(request, 404, "Resource not found", f"Customer with id {request.path_params['id']} was not found.")
    return _json(request, customer)


def _validation_problem(request: Request, errors: dict[str, list[str]]) -> JSONResponse:
    """The shape [ApiController] returns for invalid input (ValidationProblemDetails)."""
    body = {
        "type": "https://tools.ietf.org/html/rfc9110#section-15.5.1",
        "title": "One or more validation errors occurred.",
        "status": 400,
        "errors": errors,
        "correlationId": request.state.correlation_id,
    }
    response = JSONResponse(body, status_code=400, media_type="application/problem+json")
    response.headers["X-Correlation-ID"] = request.state.correlation_id
    return response


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")  # about as lenient as .NET's [EmailAddress]


def _normalise_registration(value: str) -> str:
    return value.replace(" ", "").replace("-", "").upper()


@requires(ADMIN_OR_ADVISOR)
async def create_customer(request: Request) -> Response:
    body = await request.json()
    errors: dict[str, list[str]] = {}
    name, email, phone = (str(body.get(k) or "").strip() for k in ("fullName", "email", "phone"))
    if not name:
        errors["FullName"] = ["The FullName field is required."]
    elif len(name) > 200:
        errors["FullName"] = ["The field FullName must be a string with a maximum length of 200."]
    if not email:
        errors["Email"] = ["The Email field is required."]
    elif not _EMAIL.match(email) or len(email) > 256:
        errors["Email"] = ["The Email field is not a valid e-mail address."]
    if not phone:
        errors["Phone"] = ["The Phone field is required."]
    elif len(phone) > 30:
        errors["Phone"] = ["The field Phone must be a string with a maximum length of 30."]
    if errors:
        return _validation_problem(request, errors)
    data = _state(request).data
    email = email.lower()
    if any(c["email"] == email for c in data.customers.values()):
        return _problem(request, 409, "Conflict", f"A customer with email {email} already exists.")
    customer = {"id": str(uuid4()), "fullName": name, "email": email, "phone": phone, "vehicles": []}
    data.customers[customer["id"]] = customer
    response = _json(request, customer, status=201)
    response.headers["Location"] = f"/api/customers/{customer['id']}"
    return response


@requires(ADMIN_OR_ADVISOR)
async def add_vehicle(request: Request) -> Response:
    data = _state(request).data
    customer = data.customers.get(request.path_params["id"])
    if customer is None:
        return _problem(request, 404, "Resource not found", f"Customer with id {request.path_params['id']} was not found.")
    body = await request.json()
    errors: dict[str, list[str]] = {}
    make, model, reg = (str(body.get(k) or "").strip() for k in ("make", "model", "registrationNumber"))
    for field_name, value, limit in (("Make", make, 100), ("Model", model, 100), ("RegistrationNumber", reg, 20)):
        if not value:
            errors[field_name] = [f"The {field_name} field is required."]
        elif len(value) > limit:
            errors[field_name] = [f"The field {field_name} must be a string with a maximum length of {limit}."]
    year = body.get("year")
    if not isinstance(year, int) or not 1980 <= year <= 2100:
        errors["Year"] = ["The field Year must be between 1980 and 2100."]
    if errors:
        return _validation_problem(request, errors)
    if year > datetime.now(timezone.utc).year + 1:
        return _problem(request, 400, "Business rule violated", "Vehicle year is out of range.")
    normalised = _normalise_registration(reg)
    if any(v["registrationNumber"] == normalised for v in customer["vehicles"]):
        return _problem(request, 400, "Business rule violated", f"Vehicle {normalised} is already registered for this customer.")
    vehicle = {"id": str(uuid4()), "customerId": customer["id"], "make": make, "model": model, "year": year,
               "registrationNumber": normalised}
    customer["vehicles"].append(vehicle)
    return _json(request, vehicle, status=201)


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
        return _problem(request, 404, "Resource not found", f"ServiceType with id {service_type_id} was not found.")
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


def create_app(*, now: datetime | None = None, log_path: Path | None = None) -> Starlette:
    """`now` pins the seed data to a fixed moment so tests are repeatable.
    With `log_path`, a few hours of GarageFlow-style log history are written there, and every request
    the sandbox serves is appended, like GarageFlow's console log saved with `docker logs -t`."""
    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/health/db", health_db),
            Route("/api/auth/login", login, methods=["POST"]),
            Route("/api/auth/me", me),
            Route("/api/appointments", list_appointments),
            Route("/api/appointments/{id}", get_appointment),
            Route("/api/appointments/{id}/history", appointment_history),
            Route("/api/customers", list_customers, methods=["GET"]),
            Route("/api/customers", create_customer, methods=["POST"]),
            Route("/api/customers/{id}", get_customer),
            Route("/api/customers/{id}/vehicles", add_vehicle, methods=["POST"]),
            Route("/api/service-types", service_types),
            Route("/api/technicians", technicians),
            Route("/api/bays", bays),
            Route("/api/availability", availability),
            Route("/api/waitlist", waitlist),
            Route("/api/audit", audit),
            Route("/_sandbox/faults", set_faults, methods=["POST"]),
        ]
    )
    moment = now or datetime.now(timezone.utc)
    app.state.sandbox = SandboxState(data=build_seed(moment))
    if log_path is not None:
        write_history(log_path, moment)
        app.state.sandbox.request_logger = RequestLogger(log_path)
    app.add_middleware(SandboxMiddleware)
    return app
