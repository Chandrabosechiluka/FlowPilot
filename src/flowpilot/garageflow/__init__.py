"""The integration layer: everything FlowPilot knows about the customer's GarageFlow system."""

from flowpilot.garageflow.client import GarageFlowClient, new_correlation_id
from flowpilot.garageflow.errors import (
    GarageFlowAuthError,
    GarageFlowError,
    GarageFlowNotFound,
    GarageFlowRequestError,
    GarageFlowUnavailable,
)

__all__ = [
    "GarageFlowClient",
    "GarageFlowError",
    "GarageFlowAuthError",
    "GarageFlowNotFound",
    "GarageFlowRequestError",
    "GarageFlowUnavailable",
    "new_correlation_id",
]
