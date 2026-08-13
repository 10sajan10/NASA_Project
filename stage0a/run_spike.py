#!/usr/bin/env python3
"""Execute the Stage-0A runtime comparison on synthetic work only."""
from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

from stage0a.conformance import CheckResult, ConformanceReport, run_common_fixture
from stage0a.providers import (
    DaskDistributedProvider,
    ParslProvider,
    ThinLocalSubprocessProvider,
    candidate_availability,
)


def _unavailable(name: str, version: str | None,
                 evidence: str) -> ConformanceReport:
    return ConformanceReport(
        provider=name,
        available=False,
        version=version,
        checks={"availability": CheckResult(False, evidence)},
        timings_s={},
        notes=[
            "No framework-specific fixture was executed; missing software is "
            "not treated as a conformance pass."
        ],
    )


def _failed(name: str, version: str | None,
            exc: BaseException) -> ConformanceReport:
    return ConformanceReport(
        provider=name,
        available=True,
        version=version,
        checks={
            "fixture_execution": CheckResult(
                False, f"{type(exc).__name__}: {exc}")
        },
        timings_s={},
        notes=["Candidate fixture failed before all checks completed."],
    )


def run_candidate(name: str, root: Path,
                  availability: dict) -> ConformanceReport:
    version = availability.get("version")
    if not availability["available"]:
        module = "distributed" if name.startswith("dask") else "parsl"
        return _unavailable(name, version, f"Python module {module!r} not installed")
    provider = None
    try:
        if name == "thin-local-subprocess":
            provider = ThinLocalSubprocessProvider(root / "thin", max_workers=2)
        elif name == "dask-distributed-local":
            provider = DaskDistributedProvider(n_workers=2)
        elif name == "parsl-local":
            provider = ParslProvider(max_threads=2)
        else:
            raise ValueError(name)
        return run_common_fixture(provider, version=version)
    except BaseException as exc:
        return _failed(name, version, exc)
    finally:
        if provider is not None:
            provider.close()


def _score(report: ConformanceReport) -> dict:
    """Conservative measured score; missing checks earn no credit."""
    check = lambda name: bool(
        report.checks.get(name) and report.checks[name].passed)
    correctness = 0
    correctness += 8 if check("caller_identity") else 0
    correctness += 7 if check("project_owned_commit") else 0
    correctness += 8 if check("late_duplicate_fencing") else 0
    correctness += 7 if check("cancellation") else 0
    correctness += 5 if check("running_work_terminated") else 0
    correctness += 5 if check("restart_honesty") else 0
    external = 0
    external += 6 if check("external_handle_roundtrip") else 0
    external += 5 if check("resource_metadata") else 0
    external += 0  # Four MPI/SLURM points intentionally NOT TESTED.
    external += 5 if check("cancellation") else 0
    dynamic = 0
    dynamic += 8 if check("bounded_admission") else 0
    dynamic += 7 if check("independent_dag") else 0
    # These two categories include explicit ADR judgment because a five-day
    # spike cannot derive maintenance burden from a scalar fixture.
    fixture_ran = check("independent_dag")
    if (report.provider == "thin-local-subprocess" and report.available
            and fixture_ran):
        operations = 13
        performance = 7
    elif (report.provider == "dask-distributed-local" and report.available
          and fixture_ran):
        operations = 8
        performance = 8
    elif (report.provider == "parsl-local" and report.available
          and fixture_ran):
        operations = 7
        performance = 7
    else:
        operations = 0
        performance = 0
    total = correctness + external + dynamic + operations + performance
    return {
        "correctness_restart_40": correctness,
        "external_mpi_fit_20": external,
        "dynamic_backpressure_15": dynamic,
        "implementation_operations_15": operations,
        "performance_observability_10": performance,
        "total_100": total,
        "mpi_slurm_status": "NOT_TESTED",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument(
        "--candidate", action="append",
        choices=("thin-local-subprocess", "dask-distributed-local", "parsl-local"))
    args = parser.parse_args()
    candidates = args.candidate or [
        "thin-local-subprocess", "dask-distributed-local", "parsl-local"]

    root = (args.work_root or Path(tempfile.mkdtemp(
        prefix="nasa-stage0a-", dir="/tmp"))).resolve()
    if not str(root).startswith("/tmp/"):
        raise SystemExit("--work-root must be under /tmp")
    root.mkdir(parents=True, exist_ok=True)

    availability = candidate_availability()
    reports = [run_candidate(name, root, availability[name])
               for name in candidates]
    payload = {
        "schema": "stage0a-runtime-spike-v1",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version.splitlines()[0],
            "platform": platform.platform(),
            "hostname": platform.node(),
            "private_development_node": True,
            "wrf_sfire_executed": False,
            "slurm_executed": False,
            "work_root_policy": "/tmp only",
        },
        "availability": availability,
        "reports": [report.to_dict() for report in reports],
        "scores": {report.provider: _score(report) for report in reports},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
