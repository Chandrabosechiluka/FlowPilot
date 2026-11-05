"""MCP server exposing GarageFlow as read-only tools (see server.py)."""

from flowpilot.mcp_server.server import create_server

__all__ = ["create_server"]
