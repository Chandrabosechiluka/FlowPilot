---
id: rb-triage-escalation
title: Support triage, severity and escalating to engineering
type: runbook
tags: [triage, severity, escalation, correlation-id, process]
---

# Triage and escalation

## First five minutes for any ticket

1. Health: `/health` and `/health/db`. An outage changes everything else.
2. Identify the records: customer (name, email or car registration), appointment id, time of the problem.
3. Read the record's history (who changed what, when) and any FlowPilot flags.
4. Search the logs around the time, or by the **correlation ID** if the user saw an error. Every error
   response from GarageFlow includes `correlationId`; every log line of that request carries the same ID.
5. Search past tickets and runbooks for the same symptom.

## Severity

| Severity | Meaning | Response |
|---|---|---|
| S1 | Shop cannot book or check in at all (API or database down) | Immediately; update the shop every 30 minutes |
| S2 | A feature is broken for everyone (maintenance jobs failing, login broken) | Within 2 hours |
| S3 | One customer or one record affected; workaround exists | Next working day |
| S4 | Question or how-to | Answer with the runbook |

## Escalate to engineering when

- The cause is a GarageFlow bug (behaviour contradicts the documented rules).
- Data is missing or corrupted, or a fix needs a direct database change.
- S1 lasts longer than 30 minutes.

Include: what the shop sees, when it started, the correlation IDs, the records involved, log excerpts,
what you already checked, and the impact (how many appointments or customers).

## Changing data

Support fixes that change data (rescheduling, cancelling with `allowLate`, running maintenance now,
deactivating a user) need a named human to approve them, and the reason goes into the ticket.
