# FlowPilot

An AI support and integration copilot that gets **deployed into a customer's environment**. The
customer is an auto-service shop that runs [GarageFlow](https://github.com/Chandrabosechiluka/GarageFlow)
(my ASP.NET Core + SQL Server booking system). FlowPilot connects to it, exposes it to AI agents
through the **Model Context Protocol (MCP)**, searches the shop's runbooks, past tickets and API logs,
and migrates a new shop's data from its legacy system. From Week 3 an agent uses all of this to
investigate support tickets and propose fixes that a human approves.

It is built the way a Forward Deployed Engineer works: integrate with a system you do not own, handle
its failures, respect its permissions, measure what you build, and leave the customer with something
they can run.

> Status: **Week 2**. Week 1 built a typed async GarageFlow client (JWT sign-in and refresh, safe
> retries, correlation IDs) and a read-only MCP server. Week 2 added **retrieval (RAG)** over runbooks
> and past tickets (chunking, pluggable embeddings, hybrid vector + BM25 search with Reciprocal Rank
> Fusion, in-memory or PostgreSQL/pgvector, and a retrieval evaluation), **log search** over GarageFlow's
> console logs, and a **legacy DMS migration** tool (connector factory, plan → review → apply, ledger,
> safe re-runs). 13 MCP tools; 111 tests in about 3 seconds.

## Architecture

```mermaid
flowchart LR
    subgraph Clients["AI clients"]
        CD["Claude Desktop / Claude Code"]
        AG["FlowPilot agent (Week 3)"]
    end
    subgraph FP["FlowPilot (Python)"]
        MCP["MCP server<br/>13 read-only tools + 1 prompt"]
        KB["Knowledge base<br/>chunks, embeddings,<br/>hybrid search (RRF)"]
        LG["Log search<br/>parse, filter, group"]
        C["GarageFlow client<br/>auth, retries, correlation IDs"]
        MIG["flowpilot-migrate<br/>plan / apply + ledger"]
    end
    subgraph Customer["Customer environment"]
        GF["GarageFlow API<br/>ASP.NET Core 8"]
        DB[("SQL Server")]
        LOGS["API console logs"]
        DOCS["Runbooks + past tickets"]
        LEG["Legacy DMS export (CSV)"]
    end
    PG[("PostgreSQL + pgvector<br/>(optional)")]

    CD -- MCP --> MCP
    AG -- MCP --> MCP
    MCP --> C -- "REST + JWT" --> GF --> DB
    MCP --> KB --> DOCS
    KB -. "VECTOR_STORE=pgvector" .-> PG
    MCP --> LG --> LOGS
    MIG --> LEG
    MIG --> C
```

| Folder | What it does |
|---|---|
| `src/flowpilot/garageflow/` | Integration layer: `client.py` (reads, plus create customer / add vehicle), `models.py` (GarageFlow's C# contracts), `errors.py` (ProblemDetails → typed exceptions). |
| `src/flowpilot/mcp_server/` | MCP server: `server.py` (GarageFlow tools), `knowledge_tools.py` (knowledge and log tools), `views.py` (output shaped for an LLM). |
| `src/flowpilot/knowledge/` | RAG retrieval: documents, chunking, embedders, stores (memory, pgvector), hybrid search, evaluation, `flowpilot-kb` CLI. |
| `src/flowpilot/logs/` | Parser for GarageFlow's SimpleConsole log format, search and error grouping. |
| `src/flowpilot/migration/` | Legacy migration: connectors (factory), normalisation, planner, applier with ledger, `flowpilot-migrate` CLI. |
| `src/flowpilot/sandbox/` | A fake GarageFlow with seed data, fault injection and GarageFlow-format logs (including a planted incident). |
| `knowledge/` | The shop's runbooks and resolved tickets (what FlowPilot searches). |
| `evals/retrieval.json` | 20 questions with known answers, to measure retrieval quality. |
| `samples/legacy/autoledger/` | A deliberately messy legacy export to migrate. |

## The MCP tools

| Tool | Answers |
|---|---|
| `check_garageflow_health` | Is the API up, is the database reachable, can FlowPilot sign in? |
| `list_appointments` / `get_appointment` / `get_appointment_history` | What is booked, what happened to one booking, who changed it. |
| `find_customers` / `get_customer` | Find a customer by name, email or car registration. |
| `get_shop_catalog` / `check_availability` / `list_waitlist` | Services and who can do them, free slots, waitlist offers. |
| `search_audit_log` | Shop-wide audit trail (needs an Admin account). |
| `search_knowledge` | Runbook sections and past tickets for a symptom or error, with citations. |
| `summarize_log_errors` | Warnings and errors grouped into patterns, with counts and since when. |
| `search_logs` | Log lines by correlation ID, level, text, component and time. |

Records carry rule-based **flags** (e.g. "still Booked 3 hours after its start: the no-show job may not
be running"). Together with the logs and the knowledge base, the planted sandbox incident can be traced
end to end: flags → `summarize_log_errors` (the `no-shows` job failing every minute with SQL timeouts
for the last 3 hours) → `search_knowledge` (runbook *Maintenance jobs*, ticket T-1012) → the fix needs a DBA
check and an Admin-approved `POST /api/maintenance/run`.

## Design decisions

**Integration (Week 1)**

- **Read-only tools, least privilege.** Every tool is `readOnlyHint`; FlowPilot signs in as an Advisor.
- **Retry only what is safe to repeat.** GETs are retried with exponential backoff, jitter and
  `Retry-After`; POSTs and health checks never are.
- **One correlation ID per call**, sent to GarageFlow and shown in every error, so one ID finds the
  request in both systems' logs (`search_logs` follows it).
- **Errors a model can act on**, and **tolerant models** that ignore new fields.

**Retrieval (Week 2)**

- **Chunk on headings, keep the heading path**, and embed `title + section + text` ("contextual chunk
  headers"). Tickets stay one chunk: symptom, cause and fix belong together.
- **Hybrid search.** Vector search finds meaning; BM25 finds exact error text, job names and HTTP codes.
  They are fused with **Reciprocal Rank Fusion**, which uses ranks only, so the two score scales never
  have to be calibrated against each other.
- **Pluggable embeddings** (`hash` offline baseline, `ollama` local, any OpenAI-compatible API) behind
  one interface. The index records which embedder built it and rebuilds when it changes.
- **Idempotent ingest**: content hashes per document, so only new or changed documents are re-embedded
  and deleted ones are removed.
- **Measured, not guessed.** `evals/retrieval.json` gives hit@1, hit@3 and MRR, split into questions
  that share words with the answer and paraphrases that do not. A test fails if quality regresses.
- **Right-sized storage.** In-memory for a few hundred chunks (nothing to operate); the same interface
  on PostgreSQL + pgvector (HNSW cosine index + full-text GIN index) when the corpus grows or several
  instances share one index.
- **Logs are not RAG.** They are structured and time-ordered, so they get a parser, filters and
  grouping by message pattern, not embeddings.

**Migration (Week 2)**

- **Connector factory**: one class per legacy vendor, registered by name; the planner never knows the
  vendor. Normalisation and GarageFlow's rules apply to every vendor the same way.
- **Plan → review → apply**, like `terraform plan`. The plan is a reviewable file with every create,
  link, skip, rejection (with a reason) and unmigrated field; apply refuses if the export changed.
- **Through the public API**, so every GarageFlow business rule still applies.
- **Safe to re-run**: a ledger saved atomically after each write, plus GarageFlow's own uniqueness
  (409 on a duplicate email → link, not duplicate). An outage stops the run; a re-run finishes it.

## Getting started (Windows, PowerShell)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```powershell
cd F:\projects\FlowPilot
uv sync
uv run pytest
Copy-Item .env.example .env      # then edit it (GARAGEFLOW_BASE_URL=http://localhost:5090 for the sandbox)
```

Run the sandbox and try each part:

```powershell
uv run flowpilot-sandbox                                   # terminal 1: fake GarageFlow + logs in sandbox-logs/
uv run flowpilot-kb search "no-shows not being marked"     # knowledge search from the command line
uv run flowpilot-kb eval                                   # retrieval quality report
uv run flowpilot-migrate plan --source autoledger-csv --path samples/legacy/autoledger
uv run flowpilot-migrate apply --plan migration-plan.json  # asks you to type APPLY
```

Connect Claude Desktop: [docs/connect-to-claude.md](docs/connect-to-claude.md).

Optional upgrades:

```powershell
# A real embedding model (free, local): install Ollama, then
ollama pull nomic-embed-text
# .env: EMBEDDINGS_PROVIDER=ollama   then compare: uv run flowpilot-kb eval

# PostgreSQL + pgvector instead of the in-memory index
docker compose up -d postgres
uv sync --extra postgres
# .env: VECTOR_STORE=pgvector  DATABASE_URL=postgresql://flowpilot:flowpilot@localhost:5433/flowpilot
$env:PGVECTOR_LIVE="1"; uv run pytest tests/test_live_pgvector.py -v
```

## Roadmap

| Week | Adds |
|---|---|
| 1 ✅ | GarageFlow client, read-only MCP server, sandbox, tests, CI |
| 2 ✅ | RAG retrieval (hybrid search, pgvector, eval), log search, legacy DMS migration |
| 3 | LangGraph triage agent (plan, act, observe) with an LLM, FastAPI service for tickets and approvals, human approval for write actions, guardrails |
| 4 | Evaluation suite for the agent (tool-call accuracy, groundedness) gating CI; observability (cost, latency, errors) |
| 5 | Cloud deployment with a live demo, FDE documents (discovery, solution design, runbook, handoff), demo video |
