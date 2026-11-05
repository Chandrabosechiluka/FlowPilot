"""Parsing GarageFlow's console log lines.

GarageFlow uses ASP.NET Core's SimpleConsole logger in single-line mode with scopes
(Program.cs: `SingleLine = true, IncludeScopes = true, TimestampFormat = "HH:mm:ss "`):

    09:31:02 fail: GarageFlow.Api.BackgroundJobs.MaintenanceWorker[0] Maintenance job 'no-shows' failed; ...
    09:31:05 info: GarageFlow.Api.Middleware.RequestLoggingMiddleware[0] => SpanId:..., TraceId:..., ParentId:... => ConnectionId:0HN8 => RequestPath:/api/bays RequestId:0HN8:00000001 => CorrelationId:fp-1a2b HTTP GET /api/bays responded 200 in 4 ms

`docker logs -t` puts a full UTC timestamp in front:

    2026-11-04T04:01:02.123456789Z 04:01:02 fail: ...

The console's own time has no date, so lines without the Docker prefix get their date from
`default_date`. Scopes and message are not delimited, so the message is found by removing the
known scope shapes (documented in tests/test_logs.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

LEVELS = {"trce": "trace", "dbug": "debug", "info": "info", "warn": "warning", "fail": "error", "crit": "critical"}
SEVERITY = {"trace": 0, "debug": 1, "info": 2, "warning": 3, "error": 4, "critical": 5}

_LINE = re.compile(
    r"^(?:(?P<docker>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+)?"
    r"(?P<clock>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>trce|dbug|info|warn|fail|crit):\s+"
    r"(?P<category>[\w.`+<>]+)\[(?P<event>\d+)\]\s?"
    r"(?P<rest>.*)$"
)
_CORRELATION = re.compile(r"CorrelationId(?::|=|,\s*)\s*([A-Za-z0-9_-]{1,64})")
_REQUEST_PATH = re.compile(r"RequestPath:(\S+)")
# Known scope shapes printed by ASP.NET Core, removed to leave the message.
_SCOPE_TOKENS = re.compile(
    r"^(?:SpanId:\S+,?\s*|TraceId:\S+,?\s*|ParentId:\S+\s*|ConnectionId:\S+\s*|RequestPath:\S+\s*|RequestId:\S+\s*"
    r"|CorrelationId:\S+\s*|\[CorrelationId,\s*[^\]]+\]\s*|System\.Collections\.Generic\.Dictionary\S*\s*)+"
)


@dataclass(frozen=True, slots=True)
class LogEntry:
    line_no: int
    timestamp: datetime  # UTC
    level: str  # trace | debug | info | warning | error | critical
    category: str
    event_id: int
    message: str
    correlation_id: str | None
    request_path: str | None
    raw: str

    @property
    def severity(self) -> int:
        return SEVERITY[self.level]

    @property
    def short_category(self) -> str:
        return self.category.rsplit(".", 1)[-1]


def _message(rest: str) -> str:
    if "=>" not in rest:
        return rest.strip()
    tail = rest.rsplit("=>", 1)[1].strip()
    return _SCOPE_TOKENS.sub("", tail, count=1).strip()


def parse_line(line: str, *, line_no: int = 0, default_date: date | None = None) -> LogEntry | None:
    """One entry, or None if the line is not the start of an entry (e.g. a stack-trace continuation)."""
    match = _LINE.match(line.rstrip("\r\n"))
    if not match:
        return None
    if match["docker"]:
        stamp = datetime.fromisoformat(match["docker"][:26].rstrip("Z") if "." in match["docker"] else match["docker"][:-1])
        stamp = stamp.replace(tzinfo=timezone.utc)
    else:
        day = default_date or datetime.now(timezone.utc).date()
        stamp = datetime.combine(day, time.fromisoformat(match["clock"]), timezone.utc)
    rest = match["rest"]
    correlation = _CORRELATION.search(rest)
    path = _REQUEST_PATH.search(rest)
    return LogEntry(
        line_no=line_no,
        timestamp=stamp,
        level=LEVELS[match["level"]],
        category=match["category"],
        event_id=int(match["event"]),
        message=_message(rest),
        correlation_id=correlation.group(1) if correlation else None,
        request_path=path.group(1) if path else None,
        raw=line.rstrip("\r\n"),
    )


def parse_lines(lines: Iterable[str], *, default_date: date | None = None) -> list[LogEntry]:
    """Parse a whole log. Lines that do not start a new entry are appended to the previous one's message
    (multi-line exceptions when SingleLine is off, or wrapped output)."""
    entries: list[LogEntry] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        entry = parse_line(line, line_no=n, default_date=default_date)
        if entry is not None:
            entries.append(entry)
        elif entries:
            prev = entries[-1]
            entries[-1] = LogEntry(
                prev.line_no, prev.timestamp, prev.level, prev.category, prev.event_id,
                f"{prev.message} {line.strip()}", prev.correlation_id, prev.request_path, f"{prev.raw}\n{line.rstrip()}",
            )
    return entries
