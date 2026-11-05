"""Run the fake GarageFlow: `uv run flowpilot-sandbox` (or `python -m flowpilot.sandbox`), port 5090."""

from __future__ import annotations

import argparse

import uvicorn

from flowpilot.sandbox.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake GarageFlow API for FlowPilot demos and tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5090)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
