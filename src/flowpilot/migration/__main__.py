"""Migration from the command line.

    uv run flowpilot-migrate sources
    uv run flowpilot-migrate plan  --source autoledger-csv --path samples/legacy/autoledger
    uv run flowpilot-migrate apply --plan migration-plan.json

`plan` changes nothing. `apply` asks you to type APPLY first (or pass --yes in a script).
GarageFlow settings come from .env, as for the MCP server. The account needs the Advisor or Admin role.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import anyio

from flowpilot.config import ConfigError, load_settings
from flowpilot.garageflow import GarageFlowClient, GarageFlowError
from flowpilot.migration.applier import Ledger, PlanMismatch, apply_plan
from flowpilot.migration.connectors import ConnectorError, available_connectors, create_connector
from flowpilot.migration.planner import MigrationPlan, build_plan


async def _plan(args: argparse.Namespace) -> int:
    settings = load_settings()
    connector = create_connector(args.source, path=args.path)
    async with GarageFlowClient(settings) as gf:
        plan = await build_plan(connector, gf, source_options={"path": args.path}, target=settings.garageflow_base_url)
    Path(args.out).write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    print(plan.report())
    print(f"\nPlan saved to {args.out}. Review it, then: flowpilot-migrate apply --plan {args.out}")
    return 0


async def _apply(args: argparse.Namespace) -> int:
    settings = load_settings()
    plan = MigrationPlan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    if plan.target != settings.garageflow_base_url:
        print(f"This plan was made for {plan.target}, but GARAGEFLOW_BASE_URL is {settings.garageflow_base_url}.",
              file=sys.stderr)
        return 2
    s = plan.summary
    print(f"About to write to {plan.target}: create {s.customers_to_create} customers, link {s.customers_to_link}, "
          f"add {s.vehicles_to_add} vehicles.")
    if not args.yes and input("Type APPLY to continue: ").strip() != "APPLY":
        print("Cancelled; nothing was written.")
        return 1
    connector = create_connector(plan.source_kind, **plan.source_options)
    ledger = Ledger.load(Path(args.ledger))
    async with GarageFlowClient(settings) as gf:
        report = await apply_plan(plan, connector, gf, ledger)
    print(report.summary())
    print(f"Ledger: {args.ledger}")
    return 1 if report.failed or report.stopped_early else 0


def main() -> None:
    # Windows consoles and pipes may not be UTF-8; runbook text contains characters like "→".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="flowpilot-migrate", description="Migrate a legacy DMS export into GarageFlow")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sources", help="list the supported legacy systems")
    plan = sub.add_parser("plan", help="dry run: write a plan file and a readable report")
    plan.add_argument("--source", required=True, help="connector, e.g. autoledger-csv")
    plan.add_argument("--path", required=True, help="folder with the export files")
    plan.add_argument("--out", default="migration-plan.json")
    apply = sub.add_parser("apply", help="execute a reviewed plan")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--ledger", default="migration-ledger.json")
    apply.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args()

    if args.command == "sources":
        print("\n".join(available_connectors()))
        return
    try:
        code = anyio.run(_plan if args.command == "plan" else _apply, args)
    except (ConfigError, ConnectorError, PlanMismatch, GarageFlowError) as exc:
        message = exc.for_humans() if isinstance(exc, GarageFlowError) else str(exc)
        print(f"flowpilot-migrate: {message}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
