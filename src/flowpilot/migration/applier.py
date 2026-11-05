"""Applying a reviewed plan, safely enough to run twice.

Safety rules:
1. **The data must be the data that was reviewed.** The source fingerprint is checked against the plan.
2. **A ledger records every write** (legacy id → GarageFlow id) and is saved after each one, by writing
   a temporary file and renaming it, so a crash never leaves a half-written ledger. Re-running skips
   everything in the ledger.
3. **GarageFlow's own uniqueness is the second safety net.** POSTs are not retried. If a create fails
   with a network error, we cannot know whether it happened, so the run stops. The next run's create
   gets 409 (email already exists) and *links* to that customer instead of duplicating it. Vehicles are
   checked against the customer's current vehicles before adding.
4. **One bad record does not stop the run**: business-rule rejections are reported and the run goes on.
   An outage does stop it, because every following call would fail too.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from flowpilot.garageflow import GarageFlowClient, GarageFlowError, GarageFlowRequestError, GarageFlowUnavailable
from flowpilot.migration.connectors import SourceConnector
from flowpilot.migration.planner import MigrationPlan


class PlanMismatch(RuntimeError):
    """The source data or target changed since the plan was made."""


@dataclass
class Ledger:
    path: Path
    customers: dict[str, str] = field(default_factory=dict)  # legacy key -> GarageFlow id
    vehicles: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, dict(data.get("customers", {})), dict(data.get("vehicles", {})))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"customers": self.customers, "vehicles": self.vehicles}, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)  # atomic on Windows and Linux


@dataclass
class ApplyReport:
    customers_created: list[str] = field(default_factory=list)
    customers_linked: list[str] = field(default_factory=list)
    vehicles_added: list[str] = field(default_factory=list)
    already_done: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "<id>: <error with correlation id>"
    stopped_early: str | None = None

    def summary(self) -> str:
        lines = [
            f"Customers created {len(self.customers_created)}, linked {len(self.customers_linked)}; "
            f"vehicles added {len(self.vehicles_added)}; already done (ledger) {self.already_done}; "
            f"skipped {len(self.skipped)}; failed {len(self.failed)}"
        ]
        lines += [f"  skipped {s}" for s in self.skipped]
        lines += [f"  FAILED {f}" for f in self.failed]
        if self.stopped_early:
            lines.append(f"STOPPED EARLY: {self.stopped_early}. Fix it and run apply again; finished work is skipped.")
        return "\n".join(lines)


async def apply_plan(plan: MigrationPlan, connector: SourceConnector, gf: GarageFlowClient, ledger: Ledger) -> ApplyReport:
    if connector.fingerprint() != plan.source_fingerprint:
        raise PlanMismatch("The export files changed since this plan was made. Make a new plan and review it again.")
    report = ApplyReport()

    try:
        for c in plan.customers:
            if c.key in ledger.customers:
                report.already_done += 1
                continue
            if c.action == "link":
                ledger.customers[c.key] = c.garageflow_id or ""
                report.customers_linked.append(c.key)
            else:
                try:
                    created = await gf.create_customer(full_name=c.full_name, email=c.email, phone=c.phone)
                    ledger.customers[c.key] = str(created.id)
                    report.customers_created.append(c.key)
                except GarageFlowRequestError as exc:
                    if exc.status == 409:  # created by an earlier, interrupted run (or by staff meanwhile)
                        existing = await gf.find_customer_by_email(c.email)
                        if existing is None:
                            report.failed.append(f"customer {c.key}: {exc.for_humans()}")
                            continue
                        ledger.customers[c.key] = str(existing.id)
                        report.customers_linked.append(c.key)
                    else:
                        report.failed.append(f"customer {c.key}: {exc.for_humans()}")
                        continue
            ledger.save()

        for v in plan.vehicles:
            if v.legacy_id in ledger.vehicles:
                report.already_done += 1
                continue
            if v.action == "skip":
                report.skipped.append(f"vehicle {v.legacy_id}: {v.reason}")
                continue
            owner_id = ledger.customers.get(v.owner_key)
            if not owner_id:
                report.skipped.append(f"vehicle {v.legacy_id}: its owner {v.owner_key} was not migrated")
                continue
            owner = await gf.get_customer(UUID(owner_id))
            present = next((x for x in owner.vehicles if x.registration_number == v.registration_number), None)
            if present is not None:
                ledger.vehicles[v.legacy_id] = str(present.id)
                report.skipped.append(f"vehicle {v.legacy_id}: {v.registration_number} is already on the customer")
            else:
                try:
                    added = await gf.add_vehicle(
                        UUID(owner_id), make=v.make, model=v.model, year=v.year, registration_number=v.registration_number
                    )
                    ledger.vehicles[v.legacy_id] = str(added.id)
                    report.vehicles_added.append(v.legacy_id)
                except GarageFlowRequestError as exc:
                    report.failed.append(f"vehicle {v.legacy_id}: {exc.for_humans()}")
                    continue
            ledger.save()
    except GarageFlowUnavailable as exc:
        report.stopped_early = exc.for_humans()
    except GarageFlowError as exc:  # auth problems: every following call would fail the same way
        report.stopped_early = exc.for_humans()
    finally:
        ledger.save()
    return report
