"""Execute a validated RunPlan through the existing cascade runner.

The plan carries everything the runner needs (event geometry, targets,
resolution); this module maps it onto ``scripts/run_cascade.py`` flags.
Execution deliberately goes through the same entry point humans use —
one code path, one set of logs, one lineage record — rather than a
parallel agent-only runner.

`plan_to_command` is pure (testable); `execute_plan` shells out.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_CASCADE = PROJECT_ROOT / "scripts" / "run_cascade.py"


def plan_to_command(plan: dict, *, python: str = sys.executable,
                    dry_run: bool = True,
                    extra: tuple[str, ...] = ()) -> list[str]:
    """Map a RunPlan dict (RunPlan.to_dict()) onto run_cascade.py argv."""
    ev = plan["event"]
    cmd = [python, str(RUN_CASCADE),
           "--center", str(ev["lon"]), str(ev["lat"]),
           "--radius-km", str(float(ev["radius_m"]) / 1000.0),
           "--pixel-m", str(plan["resolution_m"]),
           "--sim-hours", str(float(ev["duration_s"]) / 3600.0),
           "--targets", ",".join(plan["targets"]),
           "--np", str(plan["budget"]["cores"]),
           ]
    if ev.get("time"):
        cmd += ["--start", ev["time"]]
    if plan["budget"].get("backend"):
        cmd += ["--backend", plan["budget"]["backend"]]
    if dry_run:
        cmd.append("--dry-run")
    cmd += list(extra)
    return cmd


def execute_plan(plan: dict, *, dry_run: bool = True,
                 extra: tuple[str, ...] = (),
                 timeout: Optional[float] = None) -> dict:
    """Run the plan via run_cascade.py; returns a JSON-safe summary.

    Defaults to ``--dry-run`` so an agent (or a human reviewing an
    agent's plan) sees the resolved DAG before consuming HPC hours.
    Pass ``dry_run=False`` to actually execute.
    """
    cmd = plan_to_command(plan, dry_run=dry_run, extra=extra)
    proc = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                          text=True, timeout=timeout)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "command": cmd,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "dry_run": dry_run,
    }
