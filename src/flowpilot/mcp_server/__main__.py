"""Start the MCP server.

    uv run flowpilot-mcp                 # stdio: what Claude Desktop / Claude Code launch
    uv run flowpilot-mcp --http          # Streamable HTTP on http://127.0.0.1:8765/mcp

Logs go to stderr: with stdio, stdout carries the MCP protocol itself, so printing to stdout would
corrupt it. (A classic first-day MCP bug.)
"""

from __future__ import annotations

import argparse
import logging
import sys

from flowpilot.config import ConfigError, load_settings
from flowpilot.mcp_server.server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(description="FlowPilot MCP server for GarageFlow.")
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # one line per HTTP call is too noisy; FlowPilot logs its own
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"FlowPilot cannot start: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    server = create_server(settings=settings)
    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
