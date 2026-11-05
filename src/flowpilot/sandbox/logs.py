"""Log lines in GarageFlow's exact console format, for the sandbox.

`write_history` creates a believable last few hours of logs ending at `now`, including the incident
behind the two planted data problems in seed.py: from about 3 hours 10 minutes ago, every run of the
`no-shows` and `offer expiry` maintenance jobs fails with a SQL timeout. The API keeps answering
requests normally, which is why nobody noticed. (The real cause, a blocking database session, is not
visible in application logs at all: the agent must ask a human to check SQL Server.)

`RequestLogger` appends one line per sandbox request while it runs, with the caller's correlation ID,
so a FlowPilot correlation ID can be found in the "customer's" logs.
"""

from __future__ import annotations

import random
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKER = "GarageFlow.Api.BackgroundJobs.MaintenanceWorker"
REQUESTS = "GarageFlow.Api.Middleware.RequestLoggingMiddleware"
ERRORS = "GarageFlow.Api.Errors.ApiExceptionHandler"
LIFETIME = "Microsoft.Hosting.Lifetime"

SQL_TIMEOUT = (
    "Microsoft.Data.SqlClient.SqlException (0x80131904): Execution Timeout Expired.  The timeout period elapsed "
    "prior to completion of the operation or the server is not responding. ---> System.ComponentModel.Win32Exception "
    "(258): The wait operation timed out.    at Microsoft.Data.SqlClient.SqlConnection.OnError(SqlException exception, "
    "Boolean breakConnection, Action`1 wrapCloseInAction)    at Microsoft.EntityFrameworkCore.Storage."
    "RelationalCommand.ExecuteNonQueryAsync(RelationalCommandParameterObject parameterObject, CancellationToken "
    "cancellationToken)    at GarageFlow.Application.Services.MaintenanceService.{method}(CancellationToken ct)"
)

LEVEL_CODES = {"info": "info", "warning": "warn", "error": "fail", "debug": "dbug", "critical": "crit"}


def format_line(when: datetime, level: str, category: str, message: str, *, scopes: str = "") -> str:
    when = when.astimezone(timezone.utc)
    docker = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond:06d}000Z"
    scope_part = f" {scopes}" if scopes else ""
    return f"{docker} {when:%H:%M:%S} {LEVEL_CODES[level]}: {category}[0]{scope_part} {message}"


def request_scopes(path: str, correlation_id: str) -> str:
    conn = "0HN" + secrets.token_hex(4).upper()
    return (
        f"=> SpanId:{secrets.token_hex(8)}, TraceId:{secrets.token_hex(16)}, ParentId:0000000000000000 "
        f"=> ConnectionId:{conn} => RequestPath:{path} RequestId:{conn}:00000001 => CorrelationId:{correlation_id}"
    )


def request_line(when: datetime, method: str, path: str, status: int, ms: int, correlation_id: str) -> str:
    level = "error" if status >= 500 else "warning" if status >= 400 else "info"
    return format_line(
        when, level, REQUESTS, f"HTTP {method} {path} responded {status} in {ms} ms",
        scopes=request_scopes(path, correlation_id),
    )


def write_history(path: Path, now: datetime, *, seed: int = 7) -> int:
    """Write the last ~6 hours of logs. Returns the number of lines."""
    rng = random.Random(seed)
    start = now - timedelta(hours=6)
    incident = now - timedelta(hours=3, minutes=10)
    lines: list[tuple[datetime, str]] = []

    def add(when: datetime, text: str) -> None:
        lines.append((when, text))

    add(start, format_line(start, "info", LIFETIME, "Now listening on: http://[::]:8080"))
    add(start, format_line(start, "info", LIFETIME, "Application started. Press Ctrl+C to shut down."))
    add(start, format_line(start, "info", WORKER, "Maintenance worker started; running every 00:01:00."))

    paths = ["/api/appointments", "/api/availability", "/api/customers", "/api/service-types", "/api/bays",
             "/api/technicians", "/api/waitlist", "/api/auth/me"]
    tick = start + timedelta(seconds=5)
    while tick < now:
        if tick < incident:
            if rng.random() < 0.15:
                add(tick, format_line(tick, "info", WORKER, f"Maintenance job 'reminders' handled {rng.randint(1, 4)} item(s)."))
        else:
            for job, method in (("no-shows", "MarkNoShowsAsync"), ("offer expiry", "ExpireOffersAsync")):
                when = tick + timedelta(seconds=30 if job == "no-shows" else 61)  # each waits for the 30 s command timeout
                add(when, format_line(when, "error", WORKER,
                                      f"Maintenance job '{job}' failed; it will be retried at the next run. "
                                      + SQL_TIMEOUT.format(method=method)))
        # some normal traffic
        for _ in range(rng.randint(0, 3)):
            when = tick + timedelta(seconds=rng.randint(0, 59))
            route = rng.choice(paths)
            add(when, request_line(when, "GET", route, 200, rng.randint(3, 40), secrets.token_hex(16)))
        tick += timedelta(minutes=1)

    # a few ordinary 4xx in the morning, so not every warning is the incident
    for minutes, method, route, status in ((300, "POST", "/api/auth/login", 401), (250, "POST", "/api/appointments", 409),
                                           (130, "POST", "/api/appointments/3f2a0c1e-0000-4000-8000-000000000001/cancel", 400)):
        when = now - timedelta(minutes=minutes)
        add(when, request_line(when, method, route, status, rng.randint(5, 30), secrets.token_hex(16)))

    lines.sort(key=lambda item: item[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(text for _, text in lines) + "\n", encoding="utf-8")
    return len(lines)


class RequestLogger:
    """Appends request lines to the sandbox's log file while it runs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def log(self, method: str, path: str, status: int, ms: int, correlation_id: str) -> None:
        line = request_line(datetime.now(timezone.utc), method, path, status, ms, correlation_id)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
