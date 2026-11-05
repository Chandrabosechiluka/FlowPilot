"""What can go wrong when FlowPilot talks to GarageFlow, as a small exception hierarchy.

Every error keeps the correlation ID. When a customer reports "FlowPilot said the lookup failed",
that ID finds the exact request in GarageFlow's logs. This is the single most useful thing in a
production escalation.
"""

from __future__ import annotations

from typing import Any


class GarageFlowError(Exception):
    """Base class. `status` is the HTTP status (None when GarageFlow could not be reached at all)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        title: str | None = None,
        detail: str | None = None,
        correlation_id: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.title = title
        self.detail = detail
        self.correlation_id = correlation_id
        self.path = path

    def for_humans(self) -> str:
        """A one-paragraph explanation an agent (or a support engineer) can act on."""
        parts = [str(self)]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.path:
            parts.append(f"path {self.path}")
        if self.correlation_id:
            parts.append(f"correlationId {self.correlation_id}")
        return " | ".join(parts)


class GarageFlowUnavailable(GarageFlowError):
    """Network failure, timeout, or 5xx/429 that did not clear after the retries. Usually temporary."""


class GarageFlowAuthError(GarageFlowError):
    """401/403: FlowPilot's credentials are wrong, or its GarageFlow role is not allowed to do this."""


class GarageFlowNotFound(GarageFlowError):
    """404: the id does not exist in this customer's GarageFlow."""


class GarageFlowRequestError(GarageFlowError):
    """Any other 4xx: GarageFlow rejected the request (bad input, conflict, business rule)."""


def from_problem(status: int, body: Any, *, path: str, fallback_correlation_id: str | None) -> GarageFlowError:
    """Turn GarageFlow's ProblemDetails body ({title, detail, status, correlationId}) into an exception."""
    title = detail = correlation_id = None
    if isinstance(body, dict):
        title = body.get("title")
        detail = body.get("detail")
        correlation_id = body.get("correlationId")
    correlation_id = correlation_id or fallback_correlation_id

    message = title or f"GarageFlow returned HTTP {status}"
    if detail:
        # GarageFlow in Development puts a stack trace here; keep the first line only.
        message = f"{message}: {detail.splitlines()[0][:300]}"

    kwargs = dict(status=status, title=title, detail=detail, correlation_id=correlation_id, path=path)
    if status in (401, 403):
        return GarageFlowAuthError(message, **kwargs)
    if status == 404:
        return GarageFlowNotFound(message, **kwargs)
    if status == 429 or status >= 500:
        return GarageFlowUnavailable(message, **kwargs)
    return GarageFlowRequestError(message, **kwargs)
