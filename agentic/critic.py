"""Critic: verify a run's outputs against the plan, drive replanning.

After execution, the critic checks — deterministically — that each
planned target actually materialised in the cube, that its values are
physically sane, and that it covers enough of the event footprint. A
failed check names the producer that was bound to that variable; the
replan excludes it so the next-best candidate binds instead
(`agentic.planner.plan(exclude=...)`). This closes the loop:

    plan -> execute -> critique -> (replan with exclusions) -> ...

Everything here is auditable code, not LLM judgement; an agent can sit
on top and decide *whether* to accept a degraded result or spend more
compute, but the findings themselves are ground truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .metacatalog import MetaCatalog
from .ontology import lookup
from .planner import ComputeBudget, EventSpec, RunPlan, plan as _plan

# Physical sanity bounds per ontology variable (inclusive). Variables
# not listed only get presence/finiteness checks.
SANITY_BOUNDS: dict[str, tuple[float, float]] = {
    "fire_area": (0.0, 1.0),
    "building_damage_frac": (0.0, 1.0),
    "economic_loss_usd": (0.0, float("inf")),
    "population_exposure": (0.0, float("inf")),
    "population_density": (0.0, float("inf")),
    "asset_value_usd": (0.0, float("inf")),
    "blast_overpressure_pa": (0.0, float("inf")),
    "impact_energy_j": (0.0, float("inf")),
    "pm25_surface": (0.0, float("inf")),
    "arrival_s": (0.0, float("inf")),
}


@dataclass(frozen=True)
class Finding:
    severity: str          # "error" | "warning"
    variable: str
    producer: str          # the binding that owned this variable
    message: str


@dataclass
class CritiqueReport:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    coverage: dict[str, float] = field(default_factory=dict)
    replan_excludes: frozenset[str] = frozenset()

    def to_dict(self) -> dict:
        return {"ok": self.ok,
                "findings": [vars(f) for f in self.findings],
                "coverage": self.coverage,
                "replan_excludes": sorted(self.replan_excludes)}


def critique(plan_dict: dict, cube, *,
             coverage_threshold: float = 0.6) -> CritiqueReport:
    """Check every planned target against what the cube actually holds.

    `plan_dict` is RunPlan.to_dict(). Coverage = fraction of grid cells
    with finite values (the grid is built around the event footprint,
    so grid coverage ~ footprint coverage).
    """
    findings: list[Finding] = []
    coverage: dict[str, float] = {}
    excludes: set[str] = set()

    for var in plan_dict["targets"]:
        binding = plan_dict["bindings"].get(var, {})
        producer = binding.get("producer", "?")
        vardef = lookup(var)
        kind = vardef.kind if vardef else "static"

        if kind == "time":
            times = cube.catalog.list_times(var)
            if not times:
                findings.append(Finding("error", var, producer,
                                        "no timesteps materialised"))
                excludes.add(producer)
            continue

        if not cube.has(var):
            findings.append(Finding("error", var, producer,
                                    "target never materialised in cube"))
            excludes.add(producer)
            continue

        arr = np.asarray(cube.read_static(var), dtype="float64")
        finite = np.isfinite(arr)
        frac = float(finite.mean()) if arr.size else 0.0
        coverage[var] = round(frac, 4)
        if frac < coverage_threshold:
            findings.append(Finding(
                "warning", var, producer,
                f"only {frac:.0%} of the footprint has data "
                f"(threshold {coverage_threshold:.0%})"))
            excludes.add(producer)
            continue

        lo, hi = SANITY_BOUNDS.get(var, (-np.inf, np.inf))
        vals = arr[finite]
        if vals.size and (vals.min() < lo or vals.max() > hi):
            findings.append(Finding(
                "error", var, producer,
                f"values outside sane range [{lo}, {hi}]: "
                f"min={vals.min():.4g} max={vals.max():.4g}"))
            excludes.add(producer)

    ok = not any(f.severity == "error" for f in findings)
    return CritiqueReport(ok=ok and not excludes, findings=findings,
                          coverage=coverage,
                          replan_excludes=frozenset(excludes))


def replan(plan_dict: dict, report: CritiqueReport,
           metacat: MetaCatalog, *,
           budget: Optional[ComputeBudget] = None) -> RunPlan:
    """Re-plan the same event/targets excluding the producers the
    critic flagged. Raises PlanError when no alternative exists — the
    honest answer when the only producer for a variable failed."""
    ev = EventSpec(**plan_dict["event"])
    bud = budget or ComputeBudget(**plan_dict["budget"])
    return _plan(ev, metacat, targets=tuple(plan_dict["targets"]),
                 budget=bud, exclude=report.replan_excludes)
