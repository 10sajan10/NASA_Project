#!/usr/bin/env python3
"""Ask the cascade system a question in natural language.

The LLM planner agent (Claude) parses the question, searches the
metacatalog, dry-runs candidate plans through the deterministic
resolver, and submits a validated RunPlan. Nothing executes unless you
pass --execute (and even that defaults to the runner's plan preview;
add --for-real to consume compute).

Examples
--------
    # plan only (needs ANTHROPIC_API_KEY or `ant auth login`)
    python scripts/ask_cascade.py \\
        "What are the economic consequences of a 5 Mt asteroid airburst
         over Dallas?"

    # plan, then show the resolved DAG via run_cascade --dry-run
    python scripts/ask_cascade.py "..." --execute

    # plan, then actually run it
    python scripts/ask_cascade.py "..." --execute --for-real
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agentic import MetaCatalog
from agentic.agent import PlannerAgent
from agentic.executor import execute_plan
from models.catalog import default_catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("question", help="natural-language question")
    p.add_argument("--model", default=None,
                   help="override the planner model id")
    p.add_argument("--execute", action="store_true",
                   help="hand the validated plan to run_cascade.py "
                        "(--dry-run unless --for-real)")
    p.add_argument("--for-real", action="store_true",
                   help="with --execute: actually run, not just preview")
    p.add_argument("--plan-out", type=Path, default=None,
                   help="write the validated plan JSON here")
    args = p.parse_args(argv)

    metacat = MetaCatalog()
    default_catalog().seed_metacatalog(metacat)

    kwargs = {"model": args.model} if args.model else {}
    agent = PlannerAgent(metacat, **kwargs)
    session = agent.ask(args.question)

    print("=" * 70)
    print(session.narrative or "(no narrative)")
    print("=" * 70)
    if session.plan is None:
        print("No validated plan was produced "
              f"(stop_reason={session.stop_reason!r}, "
              f"iterations={session.iterations}).")
        return 1

    print(f"rationale : {session.rationale}")
    print(f"targets   : {', '.join(session.plan['targets'])}")
    print(f"producers : {', '.join(session.plan['producers'])}")
    print(f"resolution: {session.plan['resolution_m']} m   "
          f"est {session.plan['est_wall_s']:.0f}s on "
          f"{session.plan['budget']['cores']} cores "
          f"(fits={session.plan['fits_budget']})")
    for var, b in session.plan["bindings"].items():
        alts = f"  (alternatives: {', '.join(b['alternatives'])})" \
            if b["alternatives"] else ""
        print(f"  {var:<24} <- {b['producer']} [{b['reason']}]{alts}")

    if args.plan_out:
        args.plan_out.write_text(json.dumps(session.plan, indent=2,
                                            default=str))
        print(f"plan written to {args.plan_out}")

    if args.execute:
        result = execute_plan(session.plan, dry_run=not args.for_real)
        print(f"\n[execute] dry_run={result['dry_run']} "
              f"rc={result['returncode']}")
        print(result["stdout_tail"])
        if not result["ok"]:
            print(result["stderr_tail"], file=sys.stderr)
            return result["returncode"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
