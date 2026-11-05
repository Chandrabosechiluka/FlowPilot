"""Typed copies of GarageFlow's JSON contracts (src/GarageFlow.Application/Contracts/*.cs).

GarageFlow sends camelCase JSON (`startTimeUtc`); Python code uses snake_case (`start_time_utc`).
The alias generator maps one to the other, like System.Text.Json's camelCase naming policy.
Enums arrive as text because GarageFlow registers JsonStringEnumConverter.

If GarageFlow adds a field, these models ignore it (extra="ignore"), so FlowPilot does not break
when the customer upgrades their system: the integration is tolerant of additions.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class GfModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore", frozen=True)


class AppointmentStatus(StrEnum):
    BOOKED = "Booked"
    CHECKED_IN = "CheckedIn"
    IN_INSPECTION = "InInspection"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"
    NO_SHOW = "NoShow"


class InspectionCondition(StrEnum):
    GOOD = "Good"
    NEEDS_ATTENTION = "NeedsAttention"
    CRITICAL = "Critical"


class Skill(StrEnum):
    GENERAL = "General"
    OIL_CHANGE = "OilChange"
    BRAKES = "Brakes"
    TIRES = "Tires"
    ENGINE = "Engine"
    ELECTRICAL = "Electrical"


class UserRole(StrEnum):
    ADMIN = "Admin"
    ADVISOR = "Advisor"
    TECHNICIAN = "Technician"


class WaitlistStatus(StrEnum):
    WAITING = "Waiting"
    OFFERED = "Offered"
    ACCEPTING = "Accepting"
    FULFILLED = "Fulfilled"
    CANCELLED = "Cancelled"
    EXPIRED = "Expired"


class LoginResponse(GfModel):
    access_token: str
    expires_at_utc: datetime
    role: UserRole
    display_name: str


class InspectionItem(GfModel):
    id: UUID
    name: str
    condition: InspectionCondition
    notes: str | None = None


class Appointment(GfModel):
    id: UUID
    shop_id: UUID
    customer_id: UUID
    vehicle_id: UUID
    service_type_id: UUID
    technician_id: UUID
    bay_id: UUID
    start_time_utc: datetime
    end_time_utc: datetime
    status: AppointmentStatus
    cancellation_reason: str | None = None
    inspection_items: list[InspectionItem] = []


class Vehicle(GfModel):
    id: UUID
    customer_id: UUID
    make: str
    model: str
    year: int
    registration_number: str


class Customer(GfModel):
    id: UUID
    full_name: str
    email: str
    phone: str
    vehicles: list[Vehicle] = []


class ServiceType(GfModel):
    id: UUID
    name: str
    duration_minutes: int
    required_skill: Skill


class Technician(GfModel):
    id: UUID
    shop_id: UUID
    name: str
    skills: list[Skill]


class Bay(GfModel):
    id: UUID
    shop_id: UUID
    name: str


class Slot(GfModel):
    start_utc: datetime
    end_utc: datetime
    start_local: datetime
    end_local: datetime
    suggested_technician_id: UUID
    suggested_bay_id: UUID
    available_technician_ids: list[UUID]
    available_bay_ids: list[UUID]


class Availability(GfModel):
    date: date
    shop_id: UUID
    time_zone_id: str
    service_type_id: UUID
    duration_minutes: int
    slots: list[Slot]


class AuditLogEntry(GfModel):
    id: UUID
    entity_name: str
    entity_id: UUID
    action: str
    performed_by: str
    timestamp_utc: datetime
    details: str | None = None


class WaitlistEntry(GfModel):
    id: UUID
    shop_id: UUID
    customer_id: UUID
    vehicle_id: UUID
    service_type_id: UUID
    earliest_start_utc: datetime
    latest_start_utc: datetime
    status: WaitlistStatus
    offered_start_utc: datetime | None = None
    offer_expires_utc: datetime | None = None
    appointment_id: UUID | None = None
    created_utc: datetime


T = TypeVar("T")


class Page(GfModel, Generic[T]):
    """GarageFlow's PagedResult<T>: one page of items plus what is needed to ask for the next."""

    items: list[T]
    page: int
    page_size: int
    total_count: int
    total_pages: int = 0
    has_next: bool = False
    has_previous: bool = False
