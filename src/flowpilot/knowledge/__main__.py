"""Knowledge base from the command line.

    uv run flowpilot-kb ingest                 # index the knowledge folder, print what changed
    uv run flowpilot-kb search "no-shows not marked"
    uv run flowpilot-kb eval                   # retrieval quality on evals/retrieval.json

With VECTOR_STORE=memory (the default) the index lives only in this process, so every command
ingests first. With pgvector the index persists and `ingest` only embeds what changed.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

import anyio

from flowpilot.compat import backend_options
from flowpilot.config import ConfigError, load_knowledge_settings
from flowpilot.knowledge.evaluation import evaluate, load_cases
from flowpilot.knowledge.factory import build_knowledge_base


async def _run(args: argparse.Namespace) -> int:
    settings = load_knowledge_settings()
    kb = build_knowledge_base(settings)
    try:
        report = await kb.ingest(Path(settings.knowledge_dir))
        print(report.summary(), file=sys.stderr)
        if args.command == "search":
            for n, hit in enumerate(await kb.search(args.query, k=args.k, doc_type=args.type), start=1):
                c = hit.chunk
                print(f"{n}. [{c.doc_type}] {c.title} > {c.section}   ({c.source})")
                print(f"   score {hit.score:.4f}  vector rank {hit.vector_rank}  keyword rank {hit.keyword_rank}")
                print(textwrap.indent(textwrap.shorten(c.text, 300), "   "))
        elif args.command == "eval":
            result = await evaluate(kb, load_cases(Path(args.cases)), k=5)
            print(result.table())
            if result.hit_at(3, "wording") < args.min_hit3:
                print(f"\nFAIL: hit@3 on 'wording' cases is below {args.min_hit3:.0%}", file=sys.stderr)
                return 1
        return 0
    finally:
        await kb.store.close()


def main() -> None:
    # Windows consoles and pipes may not be UTF-8; runbook text contains characters like "→".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="flowpilot-kb", description="FlowPilot knowledge base")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="index the knowledge folder")
    search = sub.add_parser("search", help="hybrid search")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)
    search.add_argument("--type", choices=["runbook", "ticket"])
    ev = sub.add_parser("eval", help="measure retrieval quality")
    ev.add_argument("--cases", default="evals/retrieval.json")
    ev.add_argument("--min-hit3", type=float, default=0.9, help="fail below this hit@3 on 'wording' cases")
    args = parser.parse_args()
    try:
        options = backend_options(load_knowledge_settings().vector_store)
        raise SystemExit(anyio.run(_run, args, backend_options=options))
    except ConfigError as exc:
        print(f"flowpilot-kb: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
