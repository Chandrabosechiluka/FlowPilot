"""Moving a new shop's customers and vehicles from their legacy DMS into GarageFlow: plan, review, apply."""

from flowpilot.migration.applier import ApplyReport, apply_plan
from flowpilot.migration.connectors import LegacyCustomer, LegacyVehicle, SourceConnector, create_connector
from flowpilot.migration.planner import MigrationPlan, build_plan

__all__ = [
    "ApplyReport",
    "LegacyCustomer",
    "LegacyVehicle",
    "MigrationPlan",
    "SourceConnector",
    "apply_plan",
    "build_plan",
    "create_connector",
]
