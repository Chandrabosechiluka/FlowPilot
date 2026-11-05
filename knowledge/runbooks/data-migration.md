---
id: rb-data-migration
title: Migrating customers and vehicles from a legacy DMS into GarageFlow
type: runbook
tags: [migration, import, legacy, dms, csv, duplicates, onboarding]
---

# Migrating from a legacy DMS

New shops arrive with years of customers and vehicles in an old dealer management system (DMS).
FlowPilot's migration tool moves them into GarageFlow through the public API, so every GarageFlow
business rule still applies.

## Process: plan, review, apply

1. **Export** from the legacy system (for AutoLedger: `customers.csv` and `vehicles.csv`).
2. **Plan** (dry run, changes nothing): `flowpilot-migrate plan --source autoledger-csv --path <folder>`.
   It writes `migration-plan.json` and prints a summary: customers to create, customers that already
   exist in GarageFlow (matched by email), vehicles to add, rows rejected with a reason, and fields that
   have no place in GarageFlow.
3. **Review the plan with the shop.** Fix rejected rows in the export (or accept that they stay behind),
   and confirm the duplicate merges.
4. **Apply**: `flowpilot-migrate apply --plan migration-plan.json`. It refuses to run if the export files
   changed since the plan was made.
5. **Verify**: counts in GarageFlow match the plan; spot-check five customers with the shop.

## Rules the tool applies

- Email is required by GarageFlow and must be unique. Emails are compared in lower case, so
  `RAVI@X.COM` and `ravi@x.com` are the same customer (merged, not duplicated).
- Phone is required. Indian numbers are normalised to `+91 XXXXX XXXXX`.
- Registration numbers are normalised like GarageFlow does (spaces and dashes removed, upper case).
  A vehicle already on the customer is skipped.
- Vehicle years must be 1980 to next year.
- Legacy make codes are expanded (`MSIL` → Maruti Suzuki, `HYUN` → Hyundai, ...). Unknown codes are kept
  as they are, with a warning.

## Re-running is safe

Apply records every created customer and vehicle in a ledger file (`migration-ledger.json`). Running it
again skips everything already done, so an interrupted migration can simply be restarted. A customer
that exists already (HTTP 409 on create) is linked, not duplicated.

## Known gaps (product feedback)

- GarageFlow has no field for service history or odometer readings: they are listed as "not migrated".
- GarageFlow allows the same registration under two customers (a car that changed owner). The plan warns
  when that happens so the shop can decide.
