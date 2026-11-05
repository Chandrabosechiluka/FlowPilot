---
id: rb-maintenance-jobs
title: Maintenance jobs failing or not running (no-shows, reminders, waitlist offers)
type: runbook
tags: [no-show, reminders, waitlist, background-job, maintenance-worker, sql-timeout]
---

# Maintenance jobs failing or not running

GarageFlow runs a background service, `MaintenanceWorker`, inside the API process. Every
`Maintenance:IntervalSeconds` (default 60 seconds) it runs four jobs, each in its own scope and its own
try/catch:

| Job | What it does |
|---|---|
| `no-shows` | A Booked appointment not checked in `NoShowGraceMinutes` (default 30) after its start becomes `NoShow`. |
| `reminders` | Appointments starting within `ReminderLeadHours` (default 24) get one reminder. |
| `offer notifications` | New waitlist offers are announced to the customer once. |
| `offer expiry` | Offers older than 2 hours expire and the slot is offered to the next customer. |

## Symptoms

- Appointments still `Booked` long after their start time (should be `NoShow` after 30 minutes).
- Customers say they got no reminder.
- Waitlist entries stuck in `Offered` after the offer expiry time; the next customer never gets the slot.
- FlowPilot flags: "Still Booked ... the background job may not be running" or "Offer expired but the
  entry is still Offered".

## Diagnose

1. Confirm the API and database are healthy (`/health`, `/health/db`). If the database is down, follow
   the runbook "Database unreachable" first: the jobs cannot work without it.
2. Search the API logs for the worker. Each failed run logs an error from category
   `GarageFlow.Api.BackgroundJobs.MaintenanceWorker`:
   `Maintenance job 'no-shows' failed; it will be retried at the next run.` followed by the exception.
3. Read the exception:
   - `SqlException ... Execution Timeout Expired`: the job's query is blocked or too slow. Usually
     another session holds locks on `Appointments` or `WaitlistEntries` (a long report query, a manual
     `BEGIN TRAN` left open in SSMS, a large import). Check blocking sessions on SQL Server with
     `sp_who2` or `sys.dm_exec_requests` (`blocking_session_id`).
   - `DbUpdateConcurrencyException` logged as a warning ("lost a race with another change"): harmless,
     the job retries on the next run.
4. If there are **no** worker log lines at all, the worker is not running:
   - `Maintenance:Enabled` is `false` in configuration (the worker is then never registered). Look for
     the start-up line `Maintenance worker started; running every ...`. If it is missing, check the setting.
   - The API process restarted and is crash-looping (`docker ps` shows a recent start time).

## Fix

1. Remove the cause: end the blocking session (with the customer's DBA), re-enable the worker, or
   restore the database.
2. Catch up immediately instead of waiting for the next tick: an **Admin** can call
   `POST /api/maintenance/run`. It runs all four jobs once and returns
   `{noShowsMarked, remindersSent, offersNotified, offersExpired}`. This changes data, so it **needs
   human approval** when proposed by an agent.
3. Verify: no new worker errors in the logs for two intervals, stale appointments are now `NoShow`,
   expired offers are `Expired`.

## Customer communication

Tell the shop that late customers will show as no-shows within a minute, and that customers whose
waitlist offer expired may now receive a new offer. Reminders missed during the outage are not re-sent
for appointments that already started.
