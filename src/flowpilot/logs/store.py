"""A log file, loaded and re-loaded when it changes, with search and error summaries."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flowpilot.logs.parser import SEVERITY, LogEntry, parse_lines

_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEXISH = re.compile(r"\b(?:fp-)?[0-9a-f]{16,}\b", re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_STACK = re.compile(r"\s+at\s+[\w.<>`]+\(")


def message_template(message: str) -> str:
    """Group similar messages: ids and numbers become '#', stack traces are cut.
    "HTTP GET /api/appointments/3f2a... responded 404 in 12 ms" → "HTTP GET /api/appointments/# responded # in # ms".
    Quoted names (job names like 'no-shows') are kept: they are what distinguishes the errors."""
    text = _STACK.split(message, maxsplit=1)[0]
    text = _GUID.sub("#", text)
    text = _HEXISH.sub("#", text)
    text = _NUMBER.sub("#", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


@dataclass
class ErrorGroup:
    level: str
    category: str
    template: str
    count: int
    first_seen: datetime
    last_seen: datetime
    example: str
    correlation_ids: list[str] = field(default_factory=list)


class LogStore:
    def __init__(self, path: Path, *, default_date: Callable[[], date] | None = None) -> None:
        self.path = path
        self._default_date = default_date or (lambda: datetime.now(timezone.utc).date())
        self._entries: list[LogEntry] = []
        self._signature: tuple[float, int] | None = None

    @property
    def entries(self) -> list[LogEntry]:
        self.refresh()
        return self._entries

    def refresh(self) -> None:
        """Re-read the file only when its size or modification time changed."""
        if not self.path.exists():
            self._entries, self._signature = [], None
            return
        stat = self.path.stat()
        signature = (stat.st_mtime, stat.st_size)
        if signature != self._signature:
            with self.path.open(encoding="utf-8", errors="replace") as handle:
                self._entries = parse_lines(handle, default_date=self._default_date())
            self._signature = signature

    def search(
        self,
        *,
        correlation_id: str | None = None,
        min_level: str = "trace",
        text: str | None = None,
        category: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> tuple[list[LogEntry], int]:
        """Matching entries, newest first, plus the total number that matched (before `limit`)."""
        floor = SEVERITY[min_level]
        needle = text.lower() if text else None
        cat = category.lower() if category else None
        matched = [
            e
            for e in self.entries
            if (correlation_id is None or e.correlation_id == correlation_id)
            and e.severity >= floor
            and (needle is None or needle in e.message.lower())
            and (cat is None or cat in e.category.lower())
            and (since is None or e.timestamp >= since)
            and (until is None or e.timestamp < until)
        ]
        matched.sort(key=lambda e: (e.timestamp, e.line_no), reverse=True)
        return matched[:limit], len(matched)

    def summarize(self, *, since: datetime | None = None, min_level: str = "warning", top: int = 10) -> list[ErrorGroup]:
        """Warnings and errors grouped by category + message template, most frequent first."""
        entries, _ = self.search(min_level=min_level, since=since, limit=10**9)
        groups: dict[tuple[str, str, str], ErrorGroup] = {}
        for e in sorted(entries, key=lambda e: (e.timestamp, e.line_no)):
            key = (e.level, e.category, message_template(e.message))
            group = groups.get(key)
            if group is None:
                group = groups[key] = ErrorGroup(e.level, e.category, key[2], 0, e.timestamp, e.timestamp, e.message[:400])
            group.count += 1
            group.last_seen = e.timestamp
            if e.correlation_id and len(group.correlation_ids) < 3 and e.correlation_id not in group.correlation_ids:
                group.correlation_ids.append(e.correlation_id)
        return sorted(groups.values(), key=lambda g: (-SEVERITY[g.level], -g.count, g.first_seen))[:top]

    def span(self) -> tuple[datetime, datetime] | None:
        entries = self.entries
        return (entries[0].timestamp, entries[-1].timestamp) if entries else None

    @staticmethod
    def since_minutes(minutes: int | None, now: datetime) -> datetime | None:
        return now - timedelta(minutes=minutes) if minutes else None
