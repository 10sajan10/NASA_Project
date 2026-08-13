#!/usr/bin/env python3
"""Run the deterministic, network-free Stage-0 consequence baseline.

The scientific values are a smoke/conformance fixture, not a validation of the
asteroid consequence models.  The retained manifest proves that one fixed DAG
can be bound to exact component implementations, executed locally, and checked
for deterministic outputs before the new composition/runtime stages begin.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cube.grid import SimulationGrid
from cube.store import Cube
from engine import Pipeline, PipelineRunner, SerialBackend
from models.catalog import ScenarioConfig, default_catalog, make_context


CHAIN = {"exposure", "impact_scaling", "blast_damage", "econ_loss"}
OUTPUTS = (
    "population_exposure",
    "blast_overpressure_pa",
    "building_damage_frac",
    "economic_loss_usd",
)


def _array_summary(value: np.ndarray) -> dict:
    canonical = np.ascontiguousarray(value)
    return {
        "shape": list(canonical.shape),
        "dtype": str(canonical.dtype),
        "minimum": float(np.nanmin(canonical)),
        "maximum": float(np.nanmax(canonical)),
        "sum": float(np.nansum(canonical, dtype=np.float64)),
        "sha256": hashlib.sha256(canonical.tobytes()).hexdigest(),
    }


def run(workspace: Path) -> dict:
    grid = SimulationGrid.from_center_radius(
        -96.809, 32.780, 20_000.0, 1_000.0)
    cube = Cube(workspace / "cube", grid)
    try:
        scenario = ScenarioConfig(kml=Path("unused.kml"), energy_mt=5.0)
        context = make_context(
            scenario, install_root=".", templates_dir=".")
        registry = default_catalog().build_registry(context, only=CHAIN)
        pipeline = Pipeline.from_targets(
            ["economic_loss_usd"], registry=registry,
            name="stage0-reduced-consequence")
        bound = pipeline.bind(registry)
        result = PipelineRunner(
            registry, backend=SerialBackend(), verbose=False,
        ).run(
            cube,
            bound,
            context={
                "baseline": "stage0-reduced-consequence-v1",
                "scenario": {
                    "center_lon": -96.809,
                    "center_lat": 32.780,
                    "radius_m": 20_000.0,
                    "pixel_m": 1_000.0,
                    "energy_mt": 5.0,
                },
                "requested_output": "economic_loss_usd",
            },
        )
        if not result.ok:
            errors = {s.name: s.error for s in result.steps
                      if s.status == "error"}
            raise RuntimeError(f"baseline workflow failed: {errors}")

        outputs = {name: _array_summary(cube.read_static(name))
                   for name in OUTPUTS}
        damage = cube.read_static("building_damage_frac")
        pressure = cube.read_static("blast_overpressure_pa")
        height, width = grid.shape
        center = (height // 2, width // 2)
        checks = {
            "all_outputs_present": all(cube.has(name) for name in OUTPUTS),
            "damage_bounded_0_1": bool(
                np.all(damage >= 0.0) and np.all(damage <= 1.0)),
            "pressure_center_exceeds_corner": bool(
                pressure[center] > pressure[0, 0]),
            "economic_loss_positive": outputs["economic_loss_usd"]["sum"] > 0,
        }
        if not all(checks.values()):
            raise RuntimeError(f"baseline validation failed: {checks}")

        return {
            "schema": "stage0-reference-run-v1",
            "fixture": "reduced-consequence",
            "scientific_status": "conformance_only_not_validated",
            "request": {
                "target": "economic_loss_usd",
                "scenario": "5 Mt Dallas-centered synthetic fixture",
            },
            "plan": {
                "plan_id": bound.plan_id,
                "nodes": [node.name for node in bound.nodes()],
                "edges": bound.edges(),
            },
            "execution": result.to_dict(),
            "outputs": outputs,
            "validation": checks,
        }
    finally:
        cube.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    manifest = run(args.workspace)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(args.manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
