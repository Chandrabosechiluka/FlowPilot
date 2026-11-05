"""Source connectors: one per legacy system, all producing the same neutral records.

This is the Factory pattern used for multi-vendor DMS integrations: the planner never knows which
vendor it is reading. Adding a vendor = one new class + `@register("vendor-name")`; nothing else
changes. (C# comparison: an `ISourceConnector` interface and a factory keyed by vendor name.)

A connector only *reads and reshapes*. It does not validate business rules or normalise values: that
belongs to the planner, so every vendor gets exactly the same rules.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class ConnectorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyCustomer:
    legacy_id: str
    row: int  # line in the source file, for people fixing the export
    name: str
    email: str
    phone: str
    extra: dict[str, str] = field(default_factory=dict)  # fields GarageFlow has no place for


@dataclass(frozen=True, slots=True)
class LegacyVehicle:
    legacy_id: str
    row: int
    customer_legacy_id: str
    registration: str
    make: str  # as exported (may be a vendor code like MSIL)
    model: str
    year: str  # as exported (may be "21")
    extra: dict[str, str] = field(default_factory=dict)


class SourceConnector(Protocol):
    kind: str

    def describe(self) -> str: ...

    def customers(self) -> list[LegacyCustomer]: ...

    def vehicles(self) -> list[LegacyVehicle]: ...

    def fingerprint(self) -> str:
        """Changes when the source data changes. Apply refuses to run a plan made from different data."""
        ...

    def make_codes(self) -> dict[str, str]:
        """Vendor make codes → make names (empty if the vendor exports names)."""
        ...


_REGISTRY: dict[str, Callable[..., SourceConnector]] = {}


def register(kind: str) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        _REGISTRY[kind] = cls
        cls.kind = kind
        return cls

    return decorator


def available_connectors() -> list[str]:
    return sorted(_REGISTRY)


def create_connector(kind: str, **options: object) -> SourceConnector:
    try:
        factory = _REGISTRY[kind]
    except KeyError as exc:
        raise ConnectorError(f"Unknown source {kind!r}. Available: {', '.join(available_connectors())}") from exc
    return factory(**options)


def _read_csv(path: Path, required: set[str]) -> list[tuple[int, dict[str, str]]]:
    if not path.exists():
        raise ConnectorError(f"{path} not found")
    # utf-8-sig: Excel on Windows puts a byte-order mark at the start of CSV files.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ConnectorError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
        return [(n, {k: (v or "").strip() for k, v in row.items() if k}) for n, row in enumerate(reader, start=2)]


@register("autoledger-csv")
class AutoLedgerCsvConnector:
    """AutoLedger DMS 'Customer master' and 'Vehicle master' CSV exports."""

    kind = "autoledger-csv"
    CUSTOMER_COLUMNS = {"CUST_NO", "CUST_NAME", "EMAIL_ID", "MOBILE"}
    VEHICLE_COLUMNS = {"VEH_ID", "CUST_NO", "REG_NO", "MAKE_CODE", "MODEL", "MFG_YEAR"}
    MAKE_CODES = {
        "MSIL": "Maruti Suzuki", "HYUN": "Hyundai", "TATA": "Tata", "MAHI": "Mahindra", "HOND": "Honda",
        "TOYO": "Toyota", "KIA": "Kia", "RENA": "Renault", "SKOD": "Skoda", "VW": "Volkswagen", "MG": "MG",
    }

    def __init__(self, path: str | Path) -> None:
        self.folder = Path(path)
        self.customers_file = self.folder / "customers.csv"
        self.vehicles_file = self.folder / "vehicles.csv"

    def describe(self) -> str:
        return f"AutoLedger CSV export in {self.folder}"

    def customers(self) -> list[LegacyCustomer]:
        rows = _read_csv(self.customers_file, self.CUSTOMER_COLUMNS)
        return [
            LegacyCustomer(
                legacy_id=r["CUST_NO"], row=n, name=r["CUST_NAME"], email=r["EMAIL_ID"], phone=r["MOBILE"],
                extra={k: v for k, v in r.items() if k not in self.CUSTOMER_COLUMNS and v},
            )
            for n, r in rows
        ]

    def vehicles(self) -> list[LegacyVehicle]:
        rows = _read_csv(self.vehicles_file, self.VEHICLE_COLUMNS)
        return [
            LegacyVehicle(
                legacy_id=r["VEH_ID"], row=n, customer_legacy_id=r["CUST_NO"], registration=r["REG_NO"],
                make=r["MAKE_CODE"], model=r["MODEL"], year=r["MFG_YEAR"],
                extra={k: v for k, v in r.items() if k not in self.VEHICLE_COLUMNS and v},
            )
            for n, r in rows
        ]

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in (self.customers_file, self.vehicles_file):
            digest.update(path.read_bytes())
        return digest.hexdigest()[:16]

    def make_codes(self) -> dict[str, str]:
        return dict(self.MAKE_CODES)
