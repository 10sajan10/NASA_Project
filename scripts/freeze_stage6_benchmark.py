#!/usr/bin/env python3
"""Freeze the Stage-6 representative planning benchmark (Section 9.5).

    .venv/bin/python scripts/freeze_stage6_benchmark.py \
        --base-width 32 --levels 6 --measured-runs 30

Writes the measured profile and its Section 9.5 conformance report to
stage6/planning_benchmark_v1.json.  The result is recorded whether or not it
meets the budget; a missed budget is evidence, not a reason to shrink the graph.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stage6.benchmark import freeze_benchmark, section_9_5_report

DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "stage6" / \
    "planning_benchmark_v1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-width", type=int, default=32)
    parser.add_argument("--levels", type=int, default=6)
    parser.add_argument("--measured-runs", type=int, default=30)
    parser.add_argument("--warmup-runs", type=int, default=2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    arguments = parser.parse_args()

    profile, graph = freeze_benchmark(
        base_width=arguments.base_width, levels=arguments.levels,
        measured_runs=arguments.measured_runs,
        warmup_runs=arguments.warmup_runs)
    report = section_9_5_report(profile)
    document = {
        "schema": "stage6-planning-benchmark-v1",
        "section_9_5": report,
        "profile": profile.to_dict(),
        "graph": {
            "base_width": arguments.base_width,
            "levels": arguments.levels,
            "level_widths": list(graph.level_widths),
            "capability_count": graph.capability_count,
        },
    }
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
