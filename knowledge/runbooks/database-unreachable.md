---
id: rb-database-unreachable
title: Database unreachable (/health/db 503, 500 errors everywhere)
type: runbook
tags: [database, sql-server, 503, 500, docker, disk, connection-string, outage]
---

# Database unreachable

## Symptoms

- `GET /health` answers 200 but `GET /health/db` answers **503** "The database is not reachable."
- Every API call that touches data answers **500** "An unexpected error occurred" (the response never
  shows SQL details; look in the logs with the correlation ID).
- Logs show `Microsoft.Data.SqlClient.SqlException` with "A network-related or instance-specific error"
  or "Login failed for user 'sa'".

## Diagnose

1. Is SQL Server running? With Docker: `docker ps` should list `garageflow-sqlserver` as healthy.
   `docker logs garageflow-sqlserver --tail 50` shows why it stopped.
2. **Disk full** is the most common reason a SQL Server container stops or refuses writes: check free
   space on the host (`docker system df`, and the drive Docker Desktop uses on Windows).
3. Wrong password or connection string after a change: the error says "Login failed". The password
   lives in `.env` (`MSSQL_SA_PASSWORD`) for Docker and in user secrets / the hosting platform's
   configuration for the API. They must match.
4. Azure SQL: the server firewall must allow the App Service; paused serverless databases take up to a
   minute to resume (first requests time out, later ones succeed).

## Fix

- Start SQL Server first, wait until it is healthy, then restart the API
  (`docker compose --profile api up -d`). The API container waits for the database health check.
- Free disk space before restarting; do **not** delete the `sqlserver-data` volume (that is the data).
- After recovery, check the maintenance jobs: they failed during the outage and catch up on their next
  run (see "Maintenance jobs failing or not running").

## Escalate

Data loss, a corrupted database, or an outage longer than 30 minutes: escalate to engineering with the
time it started, `docker logs` output and the correlation IDs of failing requests.
