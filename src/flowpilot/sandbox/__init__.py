"""A fake GarageFlow for tests and demos (see app.py)."""

from flowpilot.sandbox.app import Faults, SandboxState, create_app
from flowpilot.sandbox.seed import sid

__all__ = ["create_app", "Faults", "SandboxState", "sid"]
