"""What the MCP tools return: compact, human-readable shapes built for an LLM to reason over.

GarageFlow's API answers with ids (`technicianId: 3f2a...`). An agent reading that has to make
three more calls to learn it means "Arjun Rao in Bay 2 doing a Brake Inspection". These views
resolve the names, show times in the shop's local time zone next to UTC, and add `flags`: short,
deterministic warnings about data that looks wrong. Flags are plain rules written in code, not
model guesses, so they are testable and the agent can trust them as evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from flowpilot.garageflow.models import (
    Appointment,
    AppointmentStatus,
    AuditLogEntry,
    Bay,
    Customer,
    InspectionCondition,
    ServiceType,
    Technician,
    WaitlistEntry,
    WaitlistStatus,
)

NO_SHOW_GRACE = timedelta(minutes=30)  # GarageFlow's MaintenanceWorker marks NoShow after 30 minutes


class Catalog(BaseModel):
    """Service types, technicians and bays, looked up by id."""

    service_types: dict[UUID, ServiceType]
    technicians: dict[UUID, Technician]
    bays: dict[UUID, Bay]

    def service_name(self, sid: UUID) -> str:
        s = self.service_types.get(sid)
        return s.name if s else f"unknown service {sid}"

    def technician_name(self, tid: UUID) -> str:
        t = self.technicians.get(tid)
        return t.name if t else f"unknown technician {tid}"

    def bay_name(self, bid: UUID) -> str:
        b = self.bays.get(bid)
        return b.name if b else f"unknown bay {bid}"


def to_local(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


def to_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ views


class InspectionItemView(BaseModel):
    name: str
    condition: InspectionCondition
    notes: str | None = None


class AppointmentView(BaseModel):
    id: UUID
    status: AppointmentStatus
    service: str
    technician: str
    bay: str
    customer: str = Field(description="Customer name, or the customer id if it could not be looked up")
    customer_id: UUID
    vehicle: str
    start_local: str
    end_local: str
    start_utc: str
    cancellation_reason: str | None = None
    inspection_items: list[InspectionItemView] = []
    flags: list[str] = Field(default_factory=list, description="Rule-based warnings about this record")


class AppointmentList(BaseModel):
    total_count: int
    page: int
    has_next: bool
    time_zone: str
    items: list[AppointmentView]


class CustomerView(BaseModel):
    id: UUID
    name: str
    email: str
    phone: str
    vehicles: list[str]
    vehicle_ids: list[UUID]


class CustomerSearchResult(BaseModel):
    matched_by: str = Field(description="'name or email' (GarageFlow search) or 'registration number' (FlowPilot scan)")
    total_count: int
    items: list[CustomerView]
    note: str | None = None


class TechnicianView(BaseModel):
    id: UUID
    name: str
    skills: list[str]


class ServiceTypeView(BaseModel):
    id: UUID
    name: str
    duration_minutes: int
    required_skill: str
    qualified_technicians: list[str]


class CatalogView(BaseModel):
    service_types: list[ServiceTypeView]
    technicians: list[TechnicianView]
    bays: list[str]
    flags: list[str] = []


class SlotView(BaseModel):
    start_local: str
    start_utc: str
    technicians: list[str]
    bays: list[str]


class AvailabilityView(BaseModel):
    date: str
    time_zone: str
    service: str
    duration_minutes: int
    total_free_slots: int
    slots: list[SlotView] = Field(description="The first free slots of the day (see total_free_slots)")


class WaitlistEntryView(BaseModel):
    id: UUID
    status: WaitlistStatus
    customer_id: UUID
    service: str
    window_local: str
    offered_start_local: str | None = None
    offer_expires_local: str | None = None
    appointment_id: UUID | None = None
    flags: list[str] = []


class AuditEntryView(BaseModel):
    when_local: str
    when_utc: str
    entity: str
    entity_id: UUID
    action: str
    performed_by: str
    details: str | None = None


class AuditPage(BaseModel):
    total_count: int
    page: int
    has_next: bool
    items: list[AuditEntryView]


class HealthView(BaseModel):
    garageflow_url: str
    api: str
    database: str
    signed_in_as_role: str | None = None
    note: str | None = None


# ------------------------------------------------------------- builders


def appointment_flags(a: Appointment, now: datetime) -> list[str]:
    flags = []
    if a.status == AppointmentStatus.BOOKED and now > a.start_time_utc + NO_SHOW_GRACE:
        hours = (now - a.start_time_utc).total_seconds() / 3600
        flags.append(
            f"Still Booked {hours:.1f}h after its start time. GarageFlow's maintenance job should have marked it "
            "NoShow after 30 minutes: the background job may not be running."
        )
    if a.status in (AppointmentStatus.CHECKED_IN, AppointmentStatus.IN_INSPECTION) and now > a.end_time_utc + timedelta(hours=2):
        flags.append("Still in progress more than 2 hours after its planned end time.")
    if any(i.condition == InspectionCondition.CRITICAL for i in a.inspection_items):
        flags.append("Has a Critical inspection item: make sure the customer was told.")
    return flags


def appointment_view(
    a: Appointment, catalog: Catalog, customer: Customer | None, tz: ZoneInfo, now: datetime
) -> AppointmentView:
    vehicle = f"vehicle {a.vehicle_id}"
    if customer:
        v = next((v for v in customer.vehicles if v.id == a.vehicle_id), None)
        if v:
            vehicle = f"{v.make} {v.model} ({v.registration_number})"
    return AppointmentView(
        id=a.id,
        status=a.status,
        service=catalog.service_name(a.service_type_id),
        technician=catalog.technician_name(a.technician_id),
        bay=catalog.bay_name(a.bay_id),
        customer=customer.full_name if customer else str(a.customer_id),
        customer_id=a.customer_id,
        vehicle=vehicle,
        start_local=to_local(a.start_time_utc, tz),
        end_local=to_local(a.end_time_utc, tz),
        start_utc=to_utc(a.start_time_utc),
        cancellation_reason=a.cancellation_reason,
        inspection_items=[InspectionItemView(name=i.name, condition=i.condition, notes=i.notes) for i in a.inspection_items],
        flags=appointment_flags(a, now),
    )


def customer_view(c: Customer) -> CustomerView:
    return CustomerView(
        id=c.id,
        name=c.full_name,
        email=c.email,
        phone=c.phone,
        vehicles=[f"{v.make} {v.model} {v.year} ({v.registration_number})" for v in c.vehicles],
        vehicle_ids=[v.id for v in c.vehicles],
    )


def catalog_view(catalog: Catalog) -> CatalogView:
    services = []
    flags = []
    for s in sorted(catalog.service_types.values(), key=lambda s: s.name):
        qualified = sorted(t.name for t in catalog.technicians.values() if s.required_skill in t.skills)
        if not qualified:
            flags.append(f"No technician has the skill {s.required_skill} needed for '{s.name}': it can never be booked.")
        services.append(
            ServiceTypeView(
                id=s.id, name=s.name, duration_minutes=s.duration_minutes,
                required_skill=s.required_skill.value, qualified_technicians=qualified,
            )
        )
    if not catalog.bays:
        flags.append("The shop has no bays: nothing can be booked.")
    return CatalogView(
        service_types=services,
        technicians=[TechnicianView(id=t.id, name=t.name, skills=[s.value for s in t.skills])
                     for t in sorted(catalog.technicians.values(), key=lambda t: t.name)],
        bays=sorted(b.name for b in catalog.bays.values()),
        flags=flags,
    )


def waitlist_view(w: WaitlistEntry, catalog: Catalog, tz: ZoneInfo, now: datetime) -> WaitlistEntryView:
    flags = []
    if w.status == WaitlistStatus.OFFERED and w.offer_expires_utc and w.offer_expires_utc < now:
        flags.append(
            "Offer expired but the entry is still Offered. GarageFlow's maintenance job should have expired it "
            "and passed the slot on: the background job may not be running."
        )
    return WaitlistEntryView(
        id=w.id,
        status=w.status,
        customer_id=w.customer_id,
        service=catalog.service_name(w.service_type_id),
        window_local=f"{to_local(w.earliest_start_utc, tz)} to {to_local(w.latest_start_utc, tz)}",
        offered_start_local=to_local(w.offered_start_utc, tz) if w.offered_start_utc else None,
        offer_expires_local=to_local(w.offer_expires_utc, tz) if w.offer_expires_utc else None,
        appointment_id=w.appointment_id,
        flags=flags,
    )


def audit_view(e: AuditLogEntry, tz: ZoneInfo) -> AuditEntryView:
    return AuditEntryView(
        when_local=to_local(e.timestamp_utc, tz),
        when_utc=to_utc(e.timestamp_utc),
        entity=e.entity_name,
        entity_id=e.entity_id,
        action=e.action,
        performed_by=e.performed_by,
        details=e.details,
    )
