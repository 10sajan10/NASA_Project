#!/usr/bin/env python3
"""Event-driven autonomy: watch an inbox for event JSON, plan, report.

Drop an event file into the inbox and a plan report appears in the
outbox:

    echo '{"kind": "asteroid_impact", "lat": 32.78, "lon": -96.81,
           "energy_mt": 5.0, "intent": "economic"}' \\
        > events/inbox/dallas.json
    python scripts/watch_events.py --once

Planning is always on; add --execute for a run_cascade --dry-run
preview per event, and --for-real to actually run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agentic import ComputeBudget, MetaCatalog
from agentic.events import EventWatcher
from models.catalog import default_catalog


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inbox", type=Path,
                   default=PROJECT_ROOT / "events" / "inbox")
    p.add_argument("--outbox", type=Path,
                   default=PROJECT_ROOT / "events" / "processed")
    p.add_argument("--interval", type=float, default=5.0,
                   help="poll interval in seconds")
    p.add_argument("--once", action="store_true",
                   help="process pending events and exit")
    p.add_argument("--cores", type=int, default=0,
                   help="compute budget cores (0 = all local)")
    p.add_argument("--wall-hours", type=float, default=1.0)
    p.add_argument("--execute", action="store_true",
                   help="run run_cascade --dry-run per event")
    p.add_argument("--for-real", action="store_true",
                   help="with --execute: actually run")
    args = p.parse_args(argv)

    metacat = MetaCatalog()
    default_catalog().seed_metacatalog(metacat)

    budget = ComputeBudget(wall_s=args.wall_hours * 3600.0)
    if args.cores > 0:
        budget.cores = args.cores

    watcher = EventWatcher(inbox=args.inbox, outbox=args.outbox,
                           metacat=metacat, budget=budget,
                           execute=args.execute, for_real=args.for_real)
    if args.once:
        reports = watcher.poll_once()
        print(f"processed {len(reports)} event(s) -> {args.outbox}")
        return 0
    watcher.watch(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
