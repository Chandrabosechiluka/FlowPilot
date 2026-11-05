# Week 1 study notes: integration layer + MCP server

Goal: explain every file aloud without reading it, and rebuild two parts yourself.

## Read in this order

1. `docs/python-for-csharp-devs.md`: the map from what you know.
2. `config.py`: fail-fast settings and why the password is hidden from `repr`.
3. `garageflow/models.py`: compare each model with `GarageFlow.Application/Contracts/*.cs`.
4. `garageflow/errors.py`: ProblemDetails → typed exceptions; why the correlation ID is kept.
5. `garageflow/client.py`: the heart of the week. Read `_request` line by line.
6. `sandbox/app.py` and `sandbox/seed.py`: the fake GarageFlow, its fault switches, the two planted problems.
7. `mcp_server/views.py`: why tools return names, local times and flags instead of raw JSON.
8. `mcp_server/server.py`: the tools, `tool_errors`, the prompt.
9. `tests/`: what each test proves. Run them: `uv run pytest -v`.

## Day plan

| Day | Do |
|---|---|
| 1 | Install uv, `uv sync`, run tests. Read the cheat sheet, `config.py`, `models.py`. Rewrite `ServiceType` and `Technician` models from memory. |
| 2 | `errors.py` and `client.py`. Draw the retry loop on paper: attempt, 401 path, retryable path, give-up path. |
| 3 | Run the sandbox; call it with `Invoke-RestMethod`; switch faults on and off. Read the seed and find the two planted problems yourself. |
| 4 | Run GarageFlow (.NET) and the live contract test. Connect Claude Desktop (docs/connect-to-claude.md) and ask it the example questions. Watch the server's stderr while it calls tools. |
| 5 | MCP Inspector: list tools, read the schemas, call each tool, trigger errors. Then the "do it yourself" tasks below. |
| Weekend | Self-test questions aloud; write a LinkedIn post about what you built (draft it, then ask for review). |

## Concepts to be able to explain

- **What MCP is**: a standard so any AI client can discover tools (name, description, JSON Schema)
  and call them. Like OpenAPI/Swagger, but designed for models, with tools, resources and prompts.
- **stdio vs Streamable HTTP**: child process on the same machine vs a network service. Why stdout is
  sacred with stdio.
- **Why tool descriptions and argument descriptions matter**: they are the only documentation the
  model reads. A vague description means wrong calls.
- **Why task-shaped tools beat one-tool-per-endpoint**: fewer calls, fewer chances to go wrong,
  output the model can reason about.
- **Read-only + least privilege**: what `readOnlyHint` signals; why FlowPilot is an Advisor.
- **Token caching and refresh**: the 60-second margin; why a lock (two concurrent calls must not both
  sign in); why re-login only once on 401 (a loop of 401s means bad credentials, not an expired token).
- **Safe retries**: idempotent vs non-idempotent; exponential backoff with jitter (why jitter: many
  clients retrying in sync can knock a recovering server over again); `Retry-After`; why 400/403/404
  are never retried.
- **Correlation IDs across systems**: FlowPilot's ID shows up in GarageFlow's logs because
  GarageFlow's middleware accepts a caller-supplied ID. Quote one in an escalation.
- **Errors for models**: `ToolError` vs an unhandled exception, and what the model sees in each case.
- **Rule-based flags vs LLM judgement**: deterministic, testable evidence the agent can build on.
- **Sandbox + contract test**: fast, reliable tests without the real system, and a separate check
  that the fake still matches the real thing.

## Interview story (practise saying it in 60 seconds)

> "At Solera I integrated our service app with several dealer management systems and migrated 340
> dealerships from CDK to Fortellis. FlowPilot is the same kind of work, rebuilt in public and aimed at
> AI agents. The customer runs GarageFlow, a .NET booking system. I wrote a typed Python client that
> handles sign-in, token refresh, safe retries and correlation IDs, and put an MCP server in front of
> it so any AI agent can investigate the shop's data, read-only and with least privilege. Tool output
> is shaped for a model: names, local times, and rule-based flags. For example, it spots appointments
> still Booked hours after their start, which means the no-show background job has stopped. It is
> tested against a sandbox with fault injection, plus a contract test against the real API."

## Self-test questions

1. A GET to `/api/appointments` returns 503 twice, then 200. How many HTTP requests did GarageFlow
   see, and how many different correlation IDs? Which test proves it?
2. Why is `POST /api/appointments` not retried? What would GarageFlow need for it to be safe?
3. FlowPilot's token expires in 45 seconds. What happens on the next call, and why?
4. GarageFlow is upgraded and `AppointmentResponse` gets a new field `notes`. Does FlowPilot break? Why not?
5. The model calls `search_audit_log`. Walk through every layer until the model reads the error text.
6. Why does `check_garageflow_health` not retry, when every other GET does?
7. Why does `find_customers` scan pages for a registration number, and what are the risks?
8. Where would you add a new tool `get_technician_schedule`? List the files and tests.
9. What is the difference between `structuredContent` and `content` in a tool result?
10. Why is the sandbox's search limited to name and email?

## Do it yourself

1. **New tool:** `get_technician_day(technician, date)`: one technician's appointments for a day,
   in order, with gaps between them. Reuse `_match` and `list_appointments` logic. Tests first.
2. **New flag:** an appointment `Completed` with no inspection items should be impossible in
   GarageFlow (the domain forbids it). Add a flag that reports it as "data integrity problem", and
   a sandbox test that plants one.
3. **Timeouts:** write a client test with `httpx2.MockTransport` whose handler raises
   `httpx2.ReadTimeout` twice and then returns a valid response. Prove the call succeeds after two
   retries, and that a handler which always times out ends in `GarageFlowUnavailable`.

Commit style: `feat(mcp): add get_technician_day tool`, `test(client): timeout is retried`.
