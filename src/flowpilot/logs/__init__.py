"""Searching GarageFlow's API logs: by correlation ID, level, text and time, plus error summaries.

Logs are not put into the vector index. They are structured, huge and time-ordered: the right tools
are filters and counting, not semantic similarity. Knowing when *not* to use RAG is part of the job.
"""

from flowpilot.logs.parser import LogEntry, parse_line, parse_lines
from flowpilot.logs.store import ErrorGroup, LogStore

__all__ = ["ErrorGroup", "LogEntry", "LogStore", "parse_line", "parse_lines"]
