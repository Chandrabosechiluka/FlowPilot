---
id: rb-booking-problems
title: Booking problems (no free slots, 409 conflict, cannot cancel or reschedule)
type: runbook
tags: [booking, availability, 409, conflict, cancel, reschedule, technician, skills]
---

# Booking problems

## "No slots available" for a service

`GET /api/availability?serviceTypeId=...&date=YYYY-MM-DD` only returns start times where **a
technician with the service's required skill** and **a bay** are both free for the full duration,
inside the shop's opening hours (local time, `Bootstrap:OpeningTime`/`ClosingTime`, default 09:00-18:00).

Check, in order:
1. Does any technician have the required skill? (FlowPilot `get_shop_catalog` lists qualified
   technicians per service and flags services nobody can do.) A common cause is an Admin editing a
   technician's skills, or the only qualified technician being fully booked.
2. Is the date in the past, or is the requested day outside opening hours in the **shop's** time zone?
3. Long services (General Service, 120 minutes) need a long free gap; short ones may still fit.

## 409 Conflict when booking

Booking takes a per-shop SQL Server application lock, re-checks the technician and bay, then saves.
Two people booking the same slot at the same moment: one succeeds, the other gets **409**. This is
correct behaviour, not a bug. The second person should pick another slot (refresh availability).

Repeated 409s for one user often mean a double click: the first request succeeded. Check the
appointment list before booking again.

409 is also returned when editing an appointment someone else changed a moment ago (row version
check). Reload and try again.

## 400 "Business rule violated" when cancelling or rescheduling

- A `Booked` appointment cannot be cancelled or rescheduled **less than 2 hours before it starts**.
  Only an Admin can override with `allowLate: true`.
- Cancelling needs a reason (max 500 characters).
- Only `Booked` appointments can be rescheduled; `CheckedIn` ones can still be cancelled.
- Completed, Cancelled and NoShow are final states: nothing can change them.

## Appointment shows at the wrong time

See the runbook "Time zones". Usually the client sent a local time without the trailing `Z`.
