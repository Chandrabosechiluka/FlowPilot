---
id: rb-time-zones
title: Time zones (appointments at the wrong hour, 3 am bookings)
type: runbook
tags: [time-zone, utc, ist, integration, availability]
---

# Time zones

## How GarageFlow handles time

- Every time is **stored and exchanged in UTC**. API requests must send times ending in `Z`,
  e.g. `2026-11-05T04:30:00Z` (= 10:00 in India).
- Each shop has a time zone (`Asia/Kolkata` by default). Opening hours are local times in that zone.
- `GET /api/availability` takes a **local date** and returns each slot in both UTC (`startUtc`) and
  local time (`startLocal`). Book with the `startUtc` value.

## Symptom: an appointment appears 5.5 hours off

An integration (website widget, call-centre tool) sent the shop's local time as if it were UTC, or
sent a time with no zone at all. 10:00 local sent as `10:00Z` shows as 15:30 IST. Times before opening
(like 03:30) usually mean the reverse mistake.

Fix the client to convert local time to UTC before calling the API, or to book only `startUtc` values
returned by availability. Move the affected appointments with the reschedule endpoint (customer
agreement needed; it is a data change, so it needs human approval).

## Symptom: wrong day in reports

A "day" in reports must be the shop's local day: for India, a local day runs from 18:30 UTC the day
before to 18:30 UTC. Querying `fromUtc=2026-11-05T00:00:00Z` misses appointments between 00:00 and
05:30 local time. FlowPilot's `list_appointments` converts local days to the correct UTC range.

## Daylight saving

India has none, but GarageFlow uses IANA zone ids, so shops elsewhere get correct opening hours across
daylight-saving changes. Never store offsets like `+05:30` as a shop's zone.
