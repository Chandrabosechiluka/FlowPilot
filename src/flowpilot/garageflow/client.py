"""An async client for the GarageFlow REST API: the "integration layer" of FlowPilot.

What makes this production-grade rather than a few `get()` calls:

1. **Auth handled once.** It signs in with a service account, caches the JWT, refreshes it a minute
   before it expires, and signs in again (once) if GarageFlow answers 401.
2. **Retries only where safe.** GET requests are retried on network errors, timeouts, 429 and 5xx,
   with exponential backoff + jitter, honouring `Retry-After`. POSTs are never retried
   automatically: retrying "book appointment" could book it twice. (GarageFlow has no
   idempotency keys yet.)
3. **One correlation ID per call**, sent as `X-Correlation-ID`. GarageFlow accepts a caller's ID
   (CorrelationIdMiddleware), so the same ID appears in FlowPilot's logs and GarageFlow's logs.
4. **Typed results** (pydantic models) and **typed errors** (errors.py), never raw dicts.

C# comparison: this is a typed `HttpClient` wrapper with a `DelegatingHandler` for auth and
a Polly retry policy, written by hand so every step is visible.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar
from uuid import UUID

import anyio
import httpx2 as httpx
from pydantic import TypeAdapter

from flowpilot.config import Settings
from flowpilot.garageflow.errors import (
    GarageFlowError,
    GarageFlowUnavailable,
    from_problem,
)
from flowpilot.garageflow.models import (
    Appointment,
    AppointmentStatus,
    AuditLogEntry,
    Availability,
    Bay,
    Customer,
    LoginResponse,
    Page,
    ServiceType,
    Technician,
    UserRole,
    WaitlistEntry,
    WaitlistStatus,
)

log = logging.getLogger("flowpilot.garageflow")

M = TypeVar("M")

RETRYABLE_STATUS = {429, 502, 503, 504}
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
MAX_BACKOFF_SECONDS = 8.0


def new_correlation_id() -> str:
    # GarageFlow accepts letters, digits, '-' and '_' up to 64 characters.
    return f"fp-{uuid.uuid4().hex}"


def _utc_param(value: datetime) -> str:
    """GarageFlow wants UTC with a 'Z'. A naive datetime is a bug, so refuse it instead of guessing."""
    if value.tzinfo is None:
        raise ValueError("datetime parameters must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GarageFlowClient:
    """Use as `async with GarageFlowClient(settings) as gf: ...` so connections are closed."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._settings = settings
        # `transport` lets tests plug in the in-process sandbox instead of a real network.
        self._http = httpx.AsyncClient(
            base_url=settings.garageflow_base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "FlowPilot/0.1"},
        )
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires_at: datetime | None = None
        self._role: UserRole | None = None
        self._auth_lock = anyio.Lock()  # two tool calls at once must not both sign in

    async def __aenter__(self) -> GarageFlowClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def role(self) -> UserRole | None:
        """FlowPilot's own role in GarageFlow (known after the first sign-in)."""
        return self._role

    # ------------------------------------------------------------------ auth

    def _token_is_fresh(self) -> bool:
        return (
            self._token is not None
            and self._token_expires_at is not None
            and datetime.now(timezone.utc) < self._token_expires_at - TOKEN_REFRESH_MARGIN
        )

    async def _ensure_token(self, *, force: bool = False) -> str:
        async with self._auth_lock:
            if force or not self._token_is_fresh():
                await self._login()
            assert self._token is not None
            return self._token

    async def _login(self) -> None:
        correlation_id = new_correlation_id()
        try:
            response = await self._http.post(
                "/api/auth/login",
                json={"email": self._settings.garageflow_email, "password": self._settings.garageflow_password},
                headers={"X-Correlation-ID": correlation_id},
            )
        except httpx.TransportError as exc:
            raise GarageFlowUnavailable(
                f"Could not reach GarageFlow at {self._settings.garageflow_base_url} to sign in ({type(exc).__name__})",
                correlation_id=correlation_id,
                path="/api/auth/login",
            ) from exc

        if response.status_code != 200:
            raise from_problem(
                response.status_code, _safe_json(response), path="/api/auth/login", fallback_correlation_id=correlation_id
            )
        login = LoginResponse.model_validate(response.json())
        self._token = login.access_token
        self._token_expires_at = login.expires_at_utc.astimezone(timezone.utc)
        self._role = login.role
        log.info("Signed in to GarageFlow as %s (%s)", login.display_name, login.role)

    # --------------------------------------------------------------- request

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        authenticated: bool = True,
        retry: bool = True,
    ) -> Any:
        """Send one logical request (possibly several HTTP attempts) and return the parsed JSON body."""
        correlation_id = new_correlation_id()
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        retryable = retry and method.upper() == "GET"
        max_attempts = 1 + (self._settings.max_retries if retryable else 0)
        relogged_in = False
        attempt = 0

        while True:
            attempt += 1
            headers = {"X-Correlation-ID": correlation_id}
            if authenticated:
                headers["Authorization"] = f"Bearer {await self._ensure_token()}"

            started = time.perf_counter()
            try:
                response = await self._http.request(method, path, params=clean_params, json=json, headers=headers)
            except httpx.TransportError as exc:
                if attempt < max_attempts:
                    await self._backoff(attempt, None, f"{type(exc).__name__}", method, path, correlation_id)
                    continue
                raise GarageFlowUnavailable(
                    f"GarageFlow did not respond ({type(exc).__name__}) after {attempt} attempt(s)",
                    correlation_id=correlation_id,
                    path=path,
                ) from exc

            elapsed_ms = (time.perf_counter() - started) * 1000
            log.debug("%s %s -> %s in %.0f ms [%s]", method, path, response.status_code, elapsed_ms, correlation_id)

            if response.status_code == 401 and authenticated and not relogged_in:
                # Token revoked or GarageFlow restarted with a new signing key: sign in again, once.
                relogged_in = True
                await self._ensure_token(force=True)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < max_attempts:
                await self._backoff(
                    attempt, response.headers.get("Retry-After"), f"HTTP {response.status_code}", method, path, correlation_id
                )
                continue

            if response.status_code >= 400:
                raise from_problem(
                    response.status_code, _safe_json(response), path=path, fallback_correlation_id=correlation_id
                )

            return response.json() if response.content else None

    async def _backoff(
        self, attempt: int, retry_after: str | None, reason: str, method: str, path: str, correlation_id: str
    ) -> None:
        delay = min(MAX_BACKOFF_SECONDS, 0.5 * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
        if retry_after and retry_after.isdigit():
            delay = min(MAX_BACKOFF_SECONDS, float(retry_after))
        log.warning("%s %s failed (%s), retry %d in %.1fs [%s]", method, path, reason, attempt, delay, correlation_id)
        await self._sleep(delay)

    async def _get(self, path: str, model: type[M] | Any, **params: Any) -> M:
        body = await self._request("GET", path, params=params)
        return TypeAdapter(model).validate_python(body)

    # ------------------------------------------------------- GarageFlow API

    async def health(self) -> dict[str, str]:
        """Liveness of the API and its database. Never raises: an outage is an answer, not an error.
        No retries: a health check must answer quickly and report what it sees right now."""
        result: dict[str, str] = {}
        for name, path in (("api", "/health"), ("database", "/health/db")):
            try:
                await self._request("GET", path, authenticated=False, retry=False)
                result[name] = "ok"
            except GarageFlowError as exc:
                result[name] = f"failing: {exc.for_humans()}"
        return result

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/api/auth/me")

    async def list_appointments(
        self,
        *,
        from_utc: datetime | None = None,
        to_utc: datetime | None = None,
        status: AppointmentStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Page[Appointment]:
        return await self._get(
            "/api/appointments",
            Page[Appointment],
            fromUtc=_utc_param(from_utc) if from_utc else None,
            toUtc=_utc_param(to_utc) if to_utc else None,
            status=status.value if status else None,
            page=page,
            pageSize=page_size,
        )

    async def get_appointment(self, appointment_id: UUID) -> Appointment:
        return await self._get(f"/api/appointments/{appointment_id}", Appointment)

    async def get_appointment_history(self, appointment_id: UUID) -> list[AuditLogEntry]:
        return await self._get(f"/api/appointments/{appointment_id}/history", list[AuditLogEntry])

    async def list_customers(self, *, search: str | None = None, page: int = 1, page_size: int = 20) -> Page[Customer]:
        return await self._get("/api/customers", Page[Customer], search=search, page=page, pageSize=page_size)

    async def get_customer(self, customer_id: UUID) -> Customer:
        return await self._get(f"/api/customers/{customer_id}", Customer)

    async def list_service_types(self) -> list[ServiceType]:
        return await self._get("/api/service-types", list[ServiceType])

    async def list_technicians(self) -> list[Technician]:
        return await self._get("/api/technicians", list[Technician])

    async def list_bays(self) -> list[Bay]:
        return await self._get("/api/bays", list[Bay])

    async def get_availability(
        self, *, service_type_id: UUID, on: date, technician_id: UUID | None = None
    ) -> Availability:
        return await self._get(
            "/api/availability",
            Availability,
            serviceTypeId=str(service_type_id),
            date=on.isoformat(),
            technicianId=str(technician_id) if technician_id else None,
        )

    async def list_waitlist(self, *, status: WaitlistStatus | None = None) -> list[WaitlistEntry]:
        return await self._get("/api/waitlist", list[WaitlistEntry], status=status.value if status else None)

    async def list_audit(
        self,
        *,
        entity_name: str | None = None,
        entity_id: UUID | None = None,
        performed_by: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Page[AuditLogEntry]:
        """Admin only in GarageFlow; an Advisor account gets GarageFlowAuthError (403)."""
        return await self._get(
            "/api/audit",
            Page[AuditLogEntry],
            entityName=entity_name,
            entityId=str(entity_id) if entity_id else None,
            performedBy=performed_by,
            page=page,
            pageSize=page_size,
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None

