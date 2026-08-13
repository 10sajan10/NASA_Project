#!/usr/bin/env python3
"""Run the consequence-free Stage-1 durable-kernel fixture."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.runtime.controller import WorkflowController
from engine.runtime.fixtures import two_task_graph


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run constant(21) -> scale(2) through the Stage-1 kernel")
    parser.add_argument(
        "--runtime-root",
        required=True,
        type=Path,
        help="absolute node-local POSIX directory (/tmp for a disposable demo)",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    graph = two_task_graph()
    with WorkflowController(args.runtime_root) as controller:
        run_id = controller.create_run(graph)
        state = controller.run_until_terminal(run_id, timeout_s=args.timeout)
        consumer = graph.task_by_key("deterministic-consumer")
        result = controller.output_value(run_id, consumer.task_id)
        payload = {
            "run_id": run_id,
            "plan_id": graph.plan_id,
            "state": state.value,
            "result": result,
            "runtime_root": str(controller.runtime_root),
            "filesystem_type": controller.filesystem_type,
            "claims": {
                "controller_restart": "same-node only",
                "max_inflight": 1,
                "wrf_sfire_executed": False,
                "mpi_executed": False,
                "slurm_contacted": False,
            },
        }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if state.value == "SUCCEEDED" and result == 42 else 1


if __name__ == "__main__":
    raise SystemExit(main())
