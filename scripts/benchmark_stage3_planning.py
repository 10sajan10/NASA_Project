#!/usr/bin/env python3
"""Measure the frozen domain-neutral Stage-3 conformance graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from resolution import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitRecord,
    ArtifactCommitStatus,
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    MilpSolveOptions,
    PlanningBenchmarkProfile,
    WorkflowResolver,
)
from stage3.fixtures import make_composition_fixture


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=1)
    arguments = parser.parse_args()
    fixture = make_composition_fixture()
    availability = ArtifactAvailabilitySnapshot.freeze(
        ArtifactCommitRecord(value.leaf_id, ArtifactCommitStatus.COMMITTED)
        for value in fixture.offered_artifact_leaves)
    resolver = WorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
        artifact_leaves=fixture.offered_artifact_leaves,
        availability_snapshot=availability,
    )
    solve_options = MilpSolveOptions(time_limit_s=30.0, presolve=False)
    profile = PlanningBenchmarkProfile.measure(
        lambda: resolver.resolve(
            fixture.root_uses, solve_options=solve_options),
        warmup_runs=arguments.warmups,
        measured_runs=arguments.runs,
        solve_options=solve_options,
        run_config={
            "fixture": "domain-neutral-pair-add",
            "process_state": "warm",
        },
    )
    print(json.dumps(profile.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
