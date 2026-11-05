---
id: rb-health-and-logs
title: Health checks, logs and correlation IDs
type: runbook
tags: [health, logs, correlation-id, docker, observability]
---

# Health checks, logs and correlation IDs

## Health endpoints

| Endpoint | Healthy answer | Meaning when it fails |
|---|---|---|
| `GET /health` | 200 `{"status":"Healthy"}` | The API process is down or unreachable (network, proxy, crashed container). |
| `GET /health/db` | 200 `{"database":"Reachable"}` | 503: the API is up but cannot reach SQL Server. |

Both need no sign-in.

## Where the logs are

GarageFlow writes one log line per entry to the console:

```
09:31:02 fail: GarageFlow.Api.BackgroundJobs.MaintenanceWorker[0] Maintenance job 'no-shows' failed; ...
```

- In Docker: `docker logs garageflow-api` (add `-t` for full UTC timestamps, `--since 2h` to limit).
  Save them for FlowPilot with `docker logs -t garageflow-api > logs/garageflow.log 2>&1`.
- On Azure App Service: Log stream, or App Service logs to a storage account.

Levels: `info`, `warn` (4xx responses), `fail` (5xx responses and failed background jobs), `crit`.
Every request writes one line: `HTTP GET /api/appointments responded 200 in 14 ms`. Query strings are
never logged (they can contain personal data).

## Correlation IDs

Every response has an `X-Correlation-ID` header, and every error body has `correlationId`. All log
lines written while handling that request carry the same ID. A caller may send its own ID in
`X-Correlation-ID` (letters, digits, `-`, `_`, up to 64 characters); FlowPilot sends `fp-...` IDs, so one
ID follows a request from FlowPilot into GarageFlow's logs.

When a user reports an error, ask for the correlation ID shown on screen; it finds the exact request.
