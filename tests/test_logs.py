"""Log parsing (GarageFlow's SimpleConsole format), search, summaries, and the sandbox's log history."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from flowpilot.logs import LogStore, parse_line, parse_lines
from flowpilot.logs.store import message_template
from tests.conftest import NOW

pytestmark = pytest.mark.anyio

WORKER_LINE = (
    "2026-11-04T04:01:02.123456789Z 04:01:02 fail: GarageFlow.Api.BackgroundJobs.MaintenanceWorker[0] "
    "Maintenance job 'no-shows' failed; it will be retried at the next run. Microsoft.Data.SqlClient.SqlException "
    "(0x80131904): Execution Timeout Expired.    at Microsoft.Data.SqlClient.SqlCommand.Run()"
)
REQUEST_LINE = (
    "09:31:05 warn: GarageFlow.Api.Middleware.RequestLoggingMiddleware[0] => SpanId:ab12, TraceId:cd34, "
    "ParentId:0000000000000000 => ConnectionId:0HN8 => RequestPath:/api/appointments RequestId:0HN8:00000001 "
    "=> CorrelationId:fp-1a2b HTTP POST /api/appointments responded 409 in 12 ms"
)


def test_docker_timestamped_line():
    e = parse_line(WORKER_LINE)
    assert e.timestamp == datetime(2026, 11, 4, 4, 1, 2, 123456, tzinfo=timezone.utc)
    assert (e.level, e.short_category, e.correlation_id) == ("error", "MaintenanceWorker", None)
    assert e.message.startswith("Maintenance job 'no-shows' failed")


def test_request_line_with_scopes():
    e = parse_line(REQUEST_LINE, default_date=date(2026, 11, 4))
    assert e.timestamp == datetime(2026, 11, 4, 9, 31, 5, tzinfo=timezone.utc)
    assert e.level == "warning"
    assert e.correlation_id == "fp-1a2b"
    assert e.request_path == "/api/appointments"
    assert e.message == "HTTP POST /api/appointments responded 409 in 12 ms"


def test_dictionary_scope_is_tolerated():
    """If GarageFlow's correlation scope prints as a type name (a Dictionary passed to BeginScope), the
    message is still found; the correlation ID is simply missing."""
    line = REQUEST_LINE.replace("CorrelationId:fp-1a2b", "System.Collections.Generic.Dictionary`2[System.String,System.Object]")
    e = parse_line(line)
    assert e.correlation_id is None
    assert e.message.startswith("HTTP POST")


def test_bracketed_correlation_scope():
    e = parse_line(REQUEST_LINE.replace("CorrelationId:fp-1a2b", "[CorrelationId, fp-9z]"))
    assert e.correlation_id == "fp-9z" and e.message.startswith("HTTP POST")


def test_continuation_lines_join_the_previous_entry():
    entries = parse_lines(["09:00:00 fail: X.Y[0] Boom", "   at Some.Method()", "", "09:00:01 info: X.Y[0] fine"],
                          default_date=date(2026, 11, 4))
    assert len(entries) == 2 and entries[0].message == "Boom at Some.Method()"


def test_non_log_lines_are_ignored():
    assert parse_line("random text") is None


def test_message_template_groups_similar_messages():
    a = message_template("HTTP GET /api/appointments/3f2a0c1e-0000-4000-8000-000000000001 responded 404 in 12 ms")
    b = message_template("HTTP GET /api/appointments/9e9e0c1e-0000-4000-8000-000000000002 responded 404 in 3 ms")
    assert a == b == "HTTP GET /api/appointments/# responded # in # ms"
    assert "'no-shows'" in message_template(parse_line(WORKER_LINE).message)
    assert "at Microsoft" not in message_template(parse_line(WORKER_LINE).message)


def test_sandbox_history_contains_the_incident(logs):
    groups = logs.summarize(since=NOW - timedelta(hours=4))
    top = groups[0]
    assert top.level == "error" and top.category.endswith("MaintenanceWorker")
    assert "'no-shows' failed" in top.template or "'offer expiry' failed" in top.template
    assert top.count >= 180  # every minute for about 3 hours
    started = min(g.first_seen for g in groups if g.category.endswith("MaintenanceWorker"))
    assert NOW - timedelta(hours=3, minutes=15) < started < NOW - timedelta(hours=3)


def test_search_filters_and_orders_newest_first(logs):
    errors, total = logs.search(min_level="error", category="MaintenanceWorker", limit=5)
    assert total >= 360 and len(errors) == 5
    assert errors[0].timestamp >= errors[-1].timestamp
    timeouts, _ = logs.search(text="execution timeout", limit=1)
    assert timeouts


async def test_live_request_lines_carry_flowpilots_correlation_id(gf, logs, sandbox_state):
    await gf.list_bays()
    correlation_id = sandbox_state.requests_seen[-1][2]
    lines, total = logs.search(correlation_id=correlation_id)
    assert total == 1 and lines[0].message.startswith("HTTP GET /api/bays responded 200")


def test_missing_file_is_empty_not_an_error(tmp_path):
    store = LogStore(tmp_path / "nope.log")
    assert store.entries == [] and store.span() is None


def test_file_is_reloaded_when_it_changes(tmp_path):
    path = tmp_path / "g.log"
    path.write_text("09:00:00 info: X.Y[0] one\n", encoding="utf-8")
    store = LogStore(path, default_date=lambda: date(2026, 11, 4))
    assert len(store.entries) == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write("09:00:01 fail: X.Y[0] two\n")
    assert [e.message for e in store.entries] == ["one", "two"]
