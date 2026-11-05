"""Planning a migration: a dry run that changes nothing and says exactly what apply would do.

Like `terraform plan`: the plan is a file a person reviews (and the shop signs off) before anything is
written. Apply executes the plan, not a fresh guess, and refuses if the source data has changed since.

Steps:
1. Read the legacy records through a connector (any vendor).
2. Normalise and validate every value with GarageFlow's rules; collect rejections with reasons.
3. Merge duplicates inside the export (same email, compared in lower case).
4. Compare with what GarageFlow already has: customers by email (link instead of create), vehicles by
   registration (skip if the customer already has it; warn if another customer has it).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from flowpilot.garageflow import GarageFlowClient
from flowpilot.garageflow.models import Customer
from flowpilot.migration.connectors import LegacyCustomer, SourceConnector
from flowpilot.migration.normalise import (
    Rejected,
    clean_email,
    clean_name,
    clean_phone,
    clean_registration,
    clean_year,
    expand_make,
)

PLAN_VERSION = 1


class PlannedCustomer(BaseModel):
    key: str = Field(description="Primary legacy id; vehicles refer to their owner by it")
    legacy_ids: list[str]
    action: Literal["create", "link"]
    garageflow_id: str | None = None  # known for 'link'
    full_name: str
    email: str
    phone: str
    warnings: list[str] = []


class PlannedVehicle(BaseModel):
    legacy_id: str
    owner_key: str
    action: Literal["add", "skip"]
    reason: str | None = None
    make: str
    model: str
    year: int
    registration_number: str
    warnings: list[str] = []


class RejectedRecord(BaseModel):
    kind: Literal["customer", "vehicle"]
    legacy_id: str
    row: int
    reasons: list[str]


class PlanSummary(BaseModel):
    customers_in_source: int
    customers_to_create: int
    customers_to_link: int
    duplicates_merged: int
    vehicles_in_source: int
    vehicles_to_add: int
    vehicles_to_skip: int
    rejected: int
    warnings: int


class MigrationPlan(BaseModel):
    version: int = PLAN_VERSION
    created_utc: datetime
    source_kind: str
    source_options: dict[str, str]
    source_description: str
    source_fingerprint: str
    target: str
    summary: PlanSummary
    customers: list[PlannedCustomer]
    vehicles: list[PlannedVehicle]
    rejected: list[RejectedRecord]
    not_migrated_fields: dict[str, list[str]]

    def report(self) -> str:
        s = self.summary
        lines = [
            f"Migration plan: {self.source_description} -> {self.target}",
            f"Customers: {s.customers_in_source} in export -> create {s.customers_to_create}, link to existing "
            f"{s.customers_to_link}, duplicates merged {s.duplicates_merged}",
            f"Vehicles:  {s.vehicles_in_source} in export -> add {s.vehicles_to_add}, skip {s.vehicles_to_skip}",
            f"Rejected:  {s.rejected}    Warnings: {s.warnings}",
        ]
        if self.rejected:
            lines += ["", "Rejected (fix in the export or leave behind):"]
            lines += [f"  - {r.kind} {r.legacy_id} (row {r.row}): {'; '.join(r.reasons)}" for r in self.rejected]
        warned = [(f"customer {c.key}", w) for c in self.customers for w in c.warnings]
        warned += [(f"vehicle {v.legacy_id}", w) for v in self.vehicles for w in v.warnings]
        if warned:
            lines += ["", "Warnings (will be migrated; check with the shop):"]
            lines += [f"  - {who}: {w}" for who, w in warned]
        skipped = [v for v in self.vehicles if v.action == "skip"]
        if skipped:
            lines += ["", "Skipped vehicles:"]
            lines += [f"  - {v.legacy_id} {v.registration_number}: {v.reason}" for v in skipped]
        if any(self.not_migrated_fields.values()):
            lines += ["", "Not migrated (GarageFlow has no field for them; product feedback):"]
            lines += [f"  - {kind}: {', '.join(fields)}" for kind, fields in self.not_migrated_fields.items() if fields]
        return "\n".join(lines)


async def _all_customers(gf: GarageFlowClient) -> list[Customer]:
    customers: list[Customer] = []
    page = 1
    while True:
        result = await gf.list_customers(page=page, page_size=100)
        customers += result.items
        if not result.has_next:
            return customers
        page += 1


async def build_plan(
    connector: SourceConnector,
    gf: GarageFlowClient,
    *,
    source_options: dict[str, str],
    target: str,
    now: datetime | None = None,
) -> MigrationPlan:
    now = now or datetime.now(timezone.utc)
    rejected: list[RejectedRecord] = []
    existing = await _all_customers(gf)
    by_email = {c.email.lower(): c for c in existing}
    reg_owner = {v.registration_number: c for c in existing for v in c.vehicles}

    # ---- customers: validate, then merge duplicates by email (first row wins)
    planned: dict[str, PlannedCustomer] = {}  # email -> plan
    key_of: dict[str, str] = {}  # any legacy customer id -> primary key
    merged = 0
    source_customers = connector.customers()
    for lc in source_customers:
        try:
            name, w1 = clean_name(lc.name)
            email, w2 = clean_email(lc.email)
            phone, w3 = clean_phone(lc.phone)
        except Rejected as exc:
            rejected.append(RejectedRecord(kind="customer", legacy_id=lc.legacy_id, row=lc.row, reasons=[str(exc)]))
            continue
        warnings = w1 + w2 + w3
        if email in planned:
            first = planned[email]
            merged += 1
            first.legacy_ids.append(lc.legacy_id)
            note = f"{lc.legacy_id} (row {lc.row}) is the same customer (same email); merged into {first.key}"
            if phone != first.phone:
                note += f"; its phone {phone} was not kept"
            first.warnings.append(note)
            key_of[lc.legacy_id] = first.key
            continue
        match = by_email.get(email)
        planned[email] = PlannedCustomer(
            key=lc.legacy_id,
            legacy_ids=[lc.legacy_id],
            action="link" if match else "create",
            garageflow_id=str(match.id) if match else None,
            full_name=match.full_name if match else name,
            email=email,
            phone=match.phone if match else phone,
            warnings=([f"already in GarageFlow as {match.full_name}: linked, not created"] if match else []) + warnings,
        )
        key_of[lc.legacy_id] = lc.legacy_id

    # ---- vehicles
    codes = connector.make_codes()
    owner_regs: dict[str, set[str]] = {}  # owner key -> registrations planned or existing
    for p in planned.values():
        if p.garageflow_id:
            owner_regs[p.key] = {v.registration_number for c in existing if str(c.id) == p.garageflow_id for v in c.vehicles}
        else:
            owner_regs[p.key] = set()
    rejected_customer_ids = {r.legacy_id for r in rejected}
    customer_by_key = {p.key: p for p in planned.values()}

    vehicles: list[PlannedVehicle] = []
    source_vehicles = connector.vehicles()
    for lv in source_vehicles:
        reasons: list[str] = []
        owner_key = key_of.get(lv.customer_legacy_id)
        if owner_key is None:
            if lv.customer_legacy_id in rejected_customer_ids:
                reasons.append(f"its owner {lv.customer_legacy_id} was rejected")
            else:
                reasons.append(f"owner {lv.customer_legacy_id} is not in the customer export")
        try:
            reg = clean_registration(lv.registration)
            make, make_warnings = expand_make(lv.make, codes)
            year, year_warnings = clean_year(lv.year, now=now)
            if not lv.model.strip():
                raise Rejected("model is missing")
        except Rejected as exc:
            reasons.append(str(exc))
            rejected.append(RejectedRecord(kind="vehicle", legacy_id=lv.legacy_id, row=lv.row, reasons=reasons))
            continue
        if owner_key is None:
            rejected.append(RejectedRecord(kind="vehicle", legacy_id=lv.legacy_id, row=lv.row, reasons=reasons))
            continue
        warnings = make_warnings + year_warnings
        action: Literal["add", "skip"] = "add"
        reason = None
        if reg in owner_regs[owner_key]:
            action, reason = "skip", "the customer already has this vehicle in GarageFlow"
        else:
            other = reg_owner.get(reg)
            owner = customer_by_key[owner_key]
            if other is not None and str(other.id) != owner.garageflow_id:
                warnings.append(
                    f"GarageFlow already has {reg} under another customer ({other.full_name}); "
                    "it will exist under both (sold car?): confirm with the shop"
                )
            owner_regs[owner_key].add(reg)
        vehicles.append(
            PlannedVehicle(
                legacy_id=lv.legacy_id, owner_key=owner_key, action=action, reason=reason, make=make,
                model=lv.model.strip(), year=year, registration_number=reg, warnings=warnings,
            )
        )

    customers = list(planned.values())
    not_migrated = {
        "customers": sorted({k for c in source_customers for k in c.extra}),
        "vehicles": sorted({k for v in source_vehicles for k in v.extra}),
    }
    summary = PlanSummary(
        customers_in_source=len(source_customers),
        customers_to_create=sum(c.action == "create" for c in customers),
        customers_to_link=sum(c.action == "link" for c in customers),
        duplicates_merged=merged,
        vehicles_in_source=len(source_vehicles),
        vehicles_to_add=sum(v.action == "add" for v in vehicles),
        vehicles_to_skip=sum(v.action == "skip" for v in vehicles),
        rejected=len(rejected),
        warnings=sum(len(c.warnings) for c in customers) + sum(len(v.warnings) for v in vehicles),
    )
    return MigrationPlan(
        created_utc=now,
        source_kind=connector.kind,
        source_options=source_options,
        source_description=connector.describe(),
        source_fingerprint=connector.fingerprint(),
        target=target,
        summary=summary,
        customers=customers,
        vehicles=vehicles,
        rejected=rejected,
        not_migrated_fields=not_migrated,
    )


__all__ = ["LegacyCustomer", "MigrationPlan", "build_plan"]
