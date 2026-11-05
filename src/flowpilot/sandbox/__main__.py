"""Run the fake GarageFlow: `uv run flowpilot-sandbox` (or `python -m flowpilot.sandbox`), port 5090.

It also writes GarageFlow-style logs to sandbox-logs/garageflow.log (a few hours of history, then one
line per request). Point FlowPilot at them with GARAGEFLOW_LOG_FILE=sandbox-logs/garageflow.log.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from flowpilot.sandbox.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake GarageFlow API for FlowPilot demos and tests.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5090)
    parser.add_argument("--log-file", default="sandbox-logs/garageflow.log", help="where to write GarageFlow-style logs")
    args = parser.parse_args()
    log_path = Path(args.log_file)
    app = create_app(log_path=log_path)
    print(f"Sandbox logs: {log_path.resolve()}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
