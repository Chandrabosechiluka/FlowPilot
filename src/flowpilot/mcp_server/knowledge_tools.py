"""MCP tools over the knowledge base (runbooks, past tickets) and GarageFlow's logs."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from flowpilot.knowledge.embeddings import EmbeddingError
from flowpilot.mcp_server.views import to_local, to_utc

if TYPE_CHECKING:
    from flowpilot.mcp_server.server import _Runtime

MAX_MESSAGE = 600


class KnowledgeHit(BaseModel):
    rank: int
    citation: str = Field(description="Cite this when you use the text: file and section")
    doc_id: str
    doc_type: str
    title: str
    section: str
    text: str
    matched_by: str = Field(description="'meaning and keywords', 'meaning' or 'keywords'")


class KnowledgeResult(BaseModel):
    query: str
    embedder: str
    hits: list[KnowledgeHit]
    note: str | None = None


class LogLine(BaseModel):
    time_local: str
    time_utc: str
    level: str
    source: str = Field(description="Short logger category, e.g. MaintenanceWorker")
    message: str
    correlation_id: str | None = None
    request_path: str | None = None


class LogSearchResult(BaseModel):
    total_matched: int
    returned: int
    log_covers: str | None = Field(description="Time span of the whole log file (local time)")
    lines: list[LogLine] = Field(description="Newest first")


class ErrorGroupView(BaseModel):
    level: str
    source: str
    count: int
    first_seen_local: str
    last_seen_local: str
    pattern: str = Field(description="The message with ids and numbers replaced by #")
    example: str
    correlation_ids: list[str]


class LogSummary(BaseModel):
    window: str
    groups: list[ErrorGroupView]
    note: str | None = None


def register_knowledge_tools(server: MCPServer, runtime: _Runtime) -> None:
    from flowpilot.mcp_server.server import READ_ONLY as annotations

    def kb():
        if runtime.knowledge is None:
            raise ToolError("The knowledge base is not available (it failed to load at start-up; see the server log).")
        return runtime.knowledge

    def logs():
        if runtime.logs is None:
            raise ToolError(
                "No GarageFlow log file is configured. Set GARAGEFLOW_LOG_FILE, for example to a file saved with "
                "`docker logs -t garageflow-api > logs/garageflow.log 2>&1`."
            )
        return runtime.logs

    def local(ts) -> str:
        return to_local(ts, runtime.tz)

    def covers() -> str | None:
        span = logs().span()
        return f"{local(span[0])} to {local(span[1])}" if span else None

    @server.tool(title="Search runbooks and past tickets", annotations=annotations)
    async def search_knowledge(
        query: Annotated[str, Field(min_length=3, description="The symptom, error message or question, in plain words.")],
        doc_type: Annotated[Literal["runbook", "ticket"] | None, Field(description="Only runbooks or only past tickets.")] = None,
        k: Annotated[int, Field(ge=1, le=10, description="How many passages to return.")] = 5,
    ) -> KnowledgeResult:
        """Search the shop's runbooks (how to diagnose and fix known problems) and resolved past tickets
        (what happened before and how it was fixed). Returns passages with citations. Use exact error text
        from logs as the query when you have it; it matches strongly."""
        try:
            hits = await kb().search(query, k=k, doc_type=doc_type)
        except EmbeddingError as exc:
            raise ToolError(f"Knowledge search failed: {exc}") from exc
        out = []
        for n, h in enumerate(hits, start=1):
            via = (
                "meaning and keywords" if h.vector_rank and h.keyword_rank else "meaning" if h.vector_rank else "keywords"
            )
            c = h.chunk
            out.append(KnowledgeHit(rank=n, citation=c.citation, doc_id=c.doc_id, doc_type=c.doc_type, title=c.title,
                                    section=c.section, text=c.text, matched_by=via))
        return KnowledgeResult(
            query=query, embedder=kb().embedder.name, hits=out,
            note=None if out else "Nothing matched. Try other words, or the exact error message.",
        )

    @server.tool(title="Search GarageFlow logs", annotations=annotations)
    async def search_logs(
        correlation_id: Annotated[str | None, Field(description="Every line of one request (from an error or a tool result).")] = None,
        min_level: Annotated[Literal["debug", "info", "warning", "error"], Field(description="Lowest level to include.")] = "info",
        text: Annotated[str | None, Field(description="Text the message must contain (case-insensitive).")] = None,
        source: Annotated[str | None, Field(description="Logger category contains this, e.g. MaintenanceWorker.")] = None,
        since_minutes: Annotated[int | None, Field(ge=1, le=10080, description="Only the last N minutes (default: all).")] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> LogSearchResult:
        """Search GarageFlow's API logs, newest first. Typical uses: all lines of a correlation ID; errors from
        one component; when a message first appeared."""
        since = runtime.now() - timedelta(minutes=since_minutes) if since_minutes else None
        entries, total = logs().search(
            correlation_id=correlation_id, min_level=min_level, text=text, category=source, since=since, limit=limit
        )
        return LogSearchResult(
            total_matched=total,
            returned=len(entries),
            log_covers=covers(),
            lines=[
                LogLine(time_local=local(e.timestamp), time_utc=to_utc(e.timestamp), level=e.level,
                        source=e.short_category, message=e.message[:MAX_MESSAGE], correlation_id=e.correlation_id,
                        request_path=e.request_path)
                for e in entries
            ],
        )

    @server.tool(title="Summarise log errors", annotations=annotations)
    async def summarize_log_errors(
        since_minutes: Annotated[int, Field(ge=1, le=10080, description="Look back this many minutes.")] = 240,
        min_level: Annotated[Literal["warning", "error"], Field(description="warning includes 4xx responses.")] = "warning",
    ) -> LogSummary:
        """Warnings and errors grouped into patterns with counts and first/last time seen, most serious and most
        frequent first. The fastest way to see what has been going wrong, and since when."""
        since = runtime.now() - timedelta(minutes=since_minutes)
        groups = logs().summarize(since=since, min_level=min_level)
        return LogSummary(
            window=f"last {since_minutes} minutes (log covers {covers()})",
            groups=[
                ErrorGroupView(level=g.level, source=g.category.rsplit(".", 1)[-1], count=g.count,
                               first_seen_local=local(g.first_seen), last_seen_local=local(g.last_seen),
                               pattern=g.template, example=g.example, correlation_ids=g.correlation_ids)
                for g in groups
            ],
            note=None if groups else "No warnings or errors in this window.",
        )
