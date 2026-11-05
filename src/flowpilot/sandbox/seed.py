"""Demo data for the sandbox: one shop in Hyderabad, its catalog, customers, a few days of appointments.

All ids come from uuid5 (a hash of a name), so they are the same on every run and tests can refer to
them. Times are built relative to `now`, so the data always looks current.

Two problems are planted on purpose, for the Week 3 agent to find:
1. An appointment still `Booked` although it started 3 hours ago. GarageFlow's MaintenanceWorker marks
   such appointments `NoShow` after 30 minutes, so this hints that the background job is not running.
2. A waitlist offer still `Offered` although it expired an hour ago (same root cause).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

SHOP_TZ = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 0), time(18, 0)
SLOT_STEP = timedelta(minutes=30)


def sid(name: str) -> str:
    """Stable id for a seed row, e.g. sid('customer:ravi')."""
    return str(uuid5(NAMESPACE_URL, f"garageflow-sandbox/{name}"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SHOP_ID = sid("shop:main")


@dataclass
class SandboxData:
    users: dict[str, dict[str, Any]]
    service_types: dict[str, dict[str, Any]]
    technicians: dict[str, dict[str, Any]]
    bays: dict[str, dict[str, Any]]
    customers: dict[str, dict[str, Any]]
    appointments: dict[str, dict[str, Any]]
    audit: list[dict[str, Any]]
    waitlist: list[dict[str, Any]]

    def availability(self, service: dict[str, Any], day: date, technician_id: str | None) -> dict[str, Any]:
        """Free start times on one local day, the same idea as GarageFlow's AvailabilityService (simplified)."""
        duration = timedelta(minutes=service["durationMinutes"])
        techs = [
            t
            for t in self.technicians.values()
            if service["requiredSkill"] in t["skills"] and (technician_id is None or t["id"] == technician_id)
        ]
        busy = [
            a
            for a in self.appointments.values()
            if a["status"] in ("Booked", "CheckedIn", "InInspection")
        ]

        def free(resource_key: str, resource_id: str, start: datetime, end: datetime) -> bool:
            for a in busy:
                if a[resource_key] != resource_id:
                    continue
                a_start = datetime.fromisoformat(a["startTimeUtc"].replace("Z", "+00:00"))
                a_end = datetime.fromisoformat(a["endTimeUtc"].replace("Z", "+00:00"))
                if start < a_end and a_start < end:  # half-open intervals, like GarageFlow
                    return False
            return True

        slots = []
        cursor = datetime.combine(day, OPEN, SHOP_TZ)
        closing = datetime.combine(day, CLOSE, SHOP_TZ)
        while cursor + duration <= closing:
            start, end = cursor.astimezone(timezone.utc), (cursor + duration).astimezone(timezone.utc)
            free_techs = [t["id"] for t in techs if free("technicianId", t["id"], start, end)]
            free_bays = [b["id"] for b in self.bays.values() if free("bayId", b["id"], start, end)]
            if free_techs and free_bays:
                slots.append(
                    {
                        "startUtc": iso(start),
                        "endUtc": iso(end),
                        "startLocal": cursor.isoformat(),
                        "endLocal": (cursor + duration).isoformat(),
                        "suggestedTechnicianId": free_techs[0],
                        "suggestedBayId": free_bays[0],
                        "availableTechnicianIds": free_techs,
                        "availableBayIds": free_bays,
                    }
                )
            cursor += SLOT_STEP
        return {
            "date": day.isoformat(),
            "shopId": SHOP_ID,
            "timeZoneId": "Asia/Kolkata",
            "serviceTypeId": service["id"],
            "durationMinutes": service["durationMinutes"],
            "slots": slots,
        }


def build_seed(now: datetime) -> SandboxData:
    today = now.astimezone(SHOP_TZ).date()

    def local(day_offset: int, hh: int, mm: int = 0) -> datetime:
        return datetime.combine(today + timedelta(days=day_offset), time(hh, mm), SHOP_TZ)

    users = {
        u["email"]: u
        for u in (
            {"email": "admin@garageflow.local", "displayName": "Demo Admin", "role": "Admin", "isActive": True},
            {"email": "advisor@garageflow.local", "displayName": "Demo Advisor", "role": "Advisor", "isActive": True},
            {"email": "tech@garageflow.local", "displayName": "Demo Technician", "role": "Technician", "isActive": True},
        )
    }

    service_types = {
        s["id"]: s
        for s in (
            {"id": sid("service:oil"), "name": "Oil Change", "durationMinutes": 30, "requiredSkill": "OilChange"},
            {"id": sid("service:brakes"), "name": "Brake Inspection", "durationMinutes": 60, "requiredSkill": "Brakes"},
            {"id": sid("service:tyres"), "name": "Tyre Rotation", "durationMinutes": 45, "requiredSkill": "Tires"},
            {"id": sid("service:general"), "name": "General Service", "durationMinutes": 120, "requiredSkill": "General"},
        )
    }
    technicians = {
        t["id"]: t
        for t in (
            {"id": sid("tech:arjun"), "shopId": SHOP_ID, "name": "Arjun Rao", "skills": ["General", "OilChange", "Brakes"]},
            {"id": sid("tech:meena"), "shopId": SHOP_ID, "name": "Meena Iyer", "skills": ["Tires", "OilChange"]},
            {"id": sid("tech:farhan"), "shopId": SHOP_ID, "name": "Farhan Ali", "skills": ["General", "Engine", "Electrical"]},
        )
    }
    bays = {
        b["id"]: b
        for b in (
            {"id": sid("bay:1"), "shopId": SHOP_ID, "name": "Bay 1"},
            {"id": sid("bay:2"), "shopId": SHOP_ID, "name": "Bay 2"},
            {"id": sid("bay:3"), "shopId": SHOP_ID, "name": "Bay 3"},
        )
    }

    def customer(key: str, name: str, email: str, phone: str, vehicles: list[tuple[str, str, str, int, str]]):
        cid = sid(f"customer:{key}")
        return {
            "id": cid,
            "fullName": name,
            "email": email,
            "phone": phone,
            "vehicles": [
                {"id": sid(f"vehicle:{vkey}"), "customerId": cid, "make": make, "model": model, "year": year,
                 "registrationNumber": reg}
                for vkey, make, model, year, reg in vehicles
            ],
        }

    customers = {
        c["id"]: c
        for c in (
            customer("ravi", "Ravi Kumar", "ravi.kumar@example.com", "+91 90000 11111",
                     [("ravi-swift", "Maruti Suzuki", "Swift", 2021, "TS09EX1234")]),
            customer("priya", "Priya Sharma", "priya.sharma@example.com", "+91 90000 22222",
                     [("priya-creta", "Hyundai", "Creta", 2022, "TS08FG5678")]),
            customer("sanjay", "Sanjay Reddy", "sanjay.reddy@example.com", "+91 90000 33333",
                     [("sanjay-nexon", "Tata", "Nexon EV", 2023, "TS07HJ9012"),
                      ("sanjay-city", "Honda", "City", 2018, "AP28CD4455")]),
            customer("lakshmi", "Lakshmi Devi", "lakshmi.devi@example.com", "+91 90000 44444",
                     [("lakshmi-innova", "Toyota", "Innova Crysta", 2020, "TS10KL3344")]),
            customer("imran", "Imran Khan", "imran.khan@example.com", "+91 90000 55555",
                     [("imran-xuv", "Mahindra", "XUV700", 2024, "TS11MN7788")]),
        )
    }

    appointments: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []

    def appt(key: str, cust: str, vehicle: str, service: str, tech: str, bay: str, start: datetime, status: str,
             reason: str | None = None, items: list[tuple[str, str, str | None]] | None = None):
        s = service_types[sid(f"service:{service}")]
        aid = sid(f"appointment:{key}")
        end = start + timedelta(minutes=s["durationMinutes"])
        appointments[aid] = {
            "id": aid, "shopId": SHOP_ID, "customerId": sid(f"customer:{cust}"),
            "vehicleId": sid(f"vehicle:{vehicle}"), "serviceTypeId": s["id"],
            "technicianId": sid(f"tech:{tech}"), "bayId": sid(f"bay:{bay}"),
            "startTimeUtc": iso(start), "endTimeUtc": iso(end), "status": status,
            "cancellationReason": reason,
            "inspectionItems": [
                {"id": sid(f"item:{key}:{i}"), "name": n, "condition": c, "notes": notes}
                for i, (n, c, notes) in enumerate(items or [])
            ],
        }
        booked_at = start - timedelta(days=2)
        trail: list[tuple[str, str, datetime, str | None]] = [("Booked", "advisor@garageflow.local", booked_at, None)]
        flow = {
            "CheckedIn": ["CheckedIn"],
            "InInspection": ["CheckedIn", "InInspection"],
            "Completed": ["CheckedIn", "InInspection", "Completed"],
            "Cancelled": ["Cancelled"],
            "NoShow": ["NoShow"],
        }.get(status, [])
        for i, step in enumerate(flow):
            who = "system" if step == "NoShow" else ("tech@garageflow.local" if step != "CheckedIn" else "advisor@garageflow.local")
            when = (booked_at + timedelta(hours=1)) if step == "Cancelled" else start + timedelta(minutes=5 + 20 * i)
            trail.append((step, who, when, reason if step == "Cancelled" else None))
        prev = None
        for n, (action, who, when, details) in enumerate(trail):
            audit.append({
                "id": sid(f"audit:{key}:{n}"), "entityName": "Appointment", "entityId": aid,
                "action": "Created" if action == "Booked" else "StatusChanged",
                "performedBy": who, "timestampUtc": iso(when),
                "details": details or (f"Status {prev} -> {action}" if prev else "Booked"),
            })
            prev = action

    # Yesterday
    appt("y1", "ravi", "ravi-swift", "general", "arjun", "1", local(-1, 10), "Completed",
         items=[("Engine oil", "Good", None), ("Air filter", "NeedsAttention", "Replace at next service")])
    appt("y2", "lakshmi", "lakshmi-innova", "brakes", "arjun", "2", local(-1, 14), "NoShow")
    # Today
    appt("t1", "priya", "priya-creta", "tyres", "meena", "2", local(0, 9, 30), "Completed",
         items=[("Front-left tyre", "Critical", "Tread below 2 mm; customer told")])
    appt("t2", "sanjay", "sanjay-nexon", "general", "farhan", "3", local(0, 11), "InInspection",
         items=[("12V battery", "NeedsAttention", None)])
    appt("t3", "imran", "imran-xuv", "oil", "meena", "1", local(0, 15), "Booked")
    appt("t4", "ravi", "ravi-swift", "brakes", "arjun", "1", local(0, 16), "Cancelled", reason="Customer travelling")
    # Planted problem 1: booked 3 hours ago, never checked in, still Booked (no-show job not running?)
    appt("stale", "sanjay", "sanjay-city", "oil", "arjun", "2", now.astimezone(SHOP_TZ) - timedelta(hours=3), "Booked")
    # Tomorrow
    appt("n1", "lakshmi", "lakshmi-innova", "brakes", "arjun", "1", local(1, 10), "Booked")
    appt("n2", "priya", "priya-creta", "oil", "meena", "2", local(1, 10), "Booked")
    appt("n3", "imran", "imran-xuv", "general", "farhan", "3", local(1, 13), "Booked")

    waitlist = [
        {
            "id": sid("waitlist:1"), "shopId": SHOP_ID, "customerId": sid("customer:ravi"),
            "vehicleId": sid("vehicle:ravi-swift"), "serviceTypeId": sid("service:brakes"),
            "earliestStartUtc": iso(local(1, 9)), "latestStartUtc": iso(local(1, 17)),
            "status": "Waiting", "offeredStartUtc": None, "offerExpiresUtc": None,
            "appointmentId": None, "createdUtc": iso(now - timedelta(days=1)),
        },
        # Planted problem 2: an offer that expired an hour ago but is still "Offered".
        {
            "id": sid("waitlist:2"), "shopId": SHOP_ID, "customerId": sid("customer:priya"),
            "vehicleId": sid("vehicle:priya-creta"), "serviceTypeId": sid("service:brakes"),
            "earliestStartUtc": iso(local(0, 9)), "latestStartUtc": iso(local(0, 18)),
            "status": "Offered", "offeredStartUtc": iso(local(0, 16)),
            "offerExpiresUtc": iso(now - timedelta(hours=1)), "appointmentId": None,
            "createdUtc": iso(now - timedelta(days=2)),
        },
    ]

    return SandboxData(users, service_types, technicians, bays, customers, appointments, audit, waitlist)
