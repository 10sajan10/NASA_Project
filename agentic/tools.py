"""Agent tool surface: JSON-in / JSON-out wrappers over the metacatalog
and planner.

These are the functions an LLM planner agent calls (via MCP, the Claude
Agent SDK, or any function-calling loop). Deliberately narrow: the agent
can search, dry-run plans, and estimate cost — it cannot register
producers, write files, or execute anything. Execution happens by
handing a validated RunPlan to the existing engine
(`CascadeCatalog.build_registry(..., only=plan.producer_names())` +
`Pipeline.from_targets`).

Every function returns a plain dict (JSON-safe) and never raises:
errors come back as {"ok": False, "error": ...} so the LLM gets
structured feedback it can revise against — the resolver's rejection
IS the anti-hallucination loop.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from .metacatalog import MetaCatalog
from .ontology import VOCABULARY, domains
from .planner import ComputeBudget, EventSpec, PlanError, plan


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


def _err(e: Exception) -> dict:
    out = {"ok": False, "error": str(e), "error_type": type(e).__name__}
    if isinstance(e, PlanError):
        out["variable"] = e.variable
        out["chain"] = list(e.chain)
    return out


def describe_ontology() -> dict:
    """Tool: list the controlled variable vocabulary, grouped by domain."""
    return _ok(domains=domains(),
               variables={k: {"units": v.units, "kind": v.kind,
                              "domain": v.domain,
                              "description": v.description}
                          for k, v in sorted(VOCABULARY.items())})


def search_datasets(metacat: MetaCatalog, *,
                    variable: Optional[str] = None,
                    bbox: Optional[list] = None,
                    t_start: Optional[str] = None,
                    t_end: Optional[str] = None,
                    max_native_res_m: Optional[float] = None,
                    min_trust: str = "unverified") -> dict:
    """Tool: deterministic dataset search (coverage/resolution/trust)."""
    try:
        cards = metacat.find_datasets(
            variable, bbox=tuple(bbox) if bbox else None,
            t_start=t_start, t_end=t_end,
            max_native_res_m=max_native_res_m, min_trust=min_trust)
        return _ok(count=len(cards), datasets=[asdict(c) for c in cards])
    except Exception as e:
        return _err(e)


def search_models(metacat: MetaCatalog, *,
                  produces: Optional[str] = None,
                  domain: Optional[str] = None,
                  bbox: Optional[list] = None,
                  magnitude: Optional[dict] = None,
                  fidelity: Optional[str] = None,
                  min_trust: str = "unverified") -> dict:
    """Tool: deterministic model search (variable/domain/regime/trust)."""
    try:
        cards = metacat.find_models(
            produces, domain=domain,
            bbox=tuple(bbox) if bbox else None,
            magnitude=magnitude, fidelity=fidelity, min_trust=min_trust)
        return _ok(count=len(cards), models=[asdict(c) for c in cards])
    except Exception as e:
        return _err(e)


def resolve_plan(metacat: MetaCatalog, event: dict, *,
                 targets: Optional[list] = None,
                 budget: Optional[dict] = None,
                 min_trust: str = "unverified") -> dict:
    """Tool: dry-run the deterministic planner.

    `event` is an EventSpec dict; `targets` overrides intent mapping.
    On failure the error names the unsatisfiable variable and the
    dependency chain that needed it — revise and call again.
    """
    try:
        ev = EventSpec(**event)
        bud = ComputeBudget(**budget) if budget else None
        p = plan(ev, metacat, targets=tuple(targets) if targets else None,
                 budget=bud, min_trust=min_trust)
        return _ok(plan=p.to_dict())
    except Exception as e:
        return _err(e)


def estimate_cost(metacat: MetaCatalog, event: dict, *,
                  targets: Optional[list] = None,
                  cores_options: tuple[int, ...] = (1, 4, 16, 56, 112),
                  wall_s: float = 3600.0) -> dict:
    """Tool: cost/resolution table across core counts, for negotiation."""
    rows = []
    for cores in cores_options:
        r = resolve_plan(metacat, event, targets=targets,
                         budget={"cores": cores, "wall_s": wall_s})
        if r["ok"]:
            p = r["plan"]
            rows.append({"cores": cores, "resolution_m": p["resolution_m"],
                         "est_wall_s": p["est_wall_s"],
                         "fits_budget": p["fits_budget"]})
        else:
            rows.append({"cores": cores, "error": r["error"]})
    return _ok(wall_budget_s=wall_s, rows=rows)
