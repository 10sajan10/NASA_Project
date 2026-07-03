"""Deterministic planner: EventSpec -> RunPlan.

Pure code, no LLM. Given an event and an intent (or explicit target
variables), it:

  1. maps intent to target variables,
  2. backward-chains over metacatalog cards from the targets, applying
     the deterministic filters (coverage, regime, trust) and scoring the
     surviving candidates per variable,
  3. solves resolution against the compute budget using each bound
     producer's cost model.

The output RunPlan records every choice, the alternatives that were
rejected, and why — so an autonomous run is as auditable as a
hand-configured one. An LLM agent sits *in front of* this function
(choosing intent/targets, arbitrating near-ties, reacting to
PlanError feedback); it never replaces it.

The focusing behaviour falls out of backward-chaining: asking only for
`economic_loss_usd` pulls impact -> damage -> exposure -> economy and
never touches the atmospheric chain.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from .metacatalog import (BBox, DatasetCard, MetaCatalog, ModelCard,
                          trust_rank)

# WRF-style CFL rule of thumb: dt [s] ~= 6 * dx [km] -> 0.006 s per m.
# Only used to turn (duration, resolution) into a step count for cost
# estimates; the fitted cost coefficients absorb the real constant.
DT_S_PER_RES_M = 0.006

# Candidate grid resolutions, finest first.
RESOLUTION_LADDER_M = (30.0, 100.0, 300.0, 900.0, 1500.0, 3000.0, 9000.0)

_FIDELITY_SCORE = {"scaling-law": 1, "reduced-order": 2, "full-physics": 3}

INTENT_TARGETS: dict[str, tuple[str, ...]] = {
    "full_cascade": ("arrival_s", "fire_area", "pm25_surface",
                     "economic_loss_usd"),
    "fire": ("arrival_s", "fire_area"),
    "air_quality": ("pm25_surface",),
    "economic": ("economic_loss_usd",),
    "exposure": ("population_exposure", "building_damage_frac"),
}


def targets_for_intent(intent: str) -> tuple[str, ...]:
    try:
        return INTENT_TARGETS[intent]
    except KeyError as exc:
        raise PlanError(
            f"unknown intent {intent!r}; known: {sorted(INTENT_TARGETS)}"
        ) from exc


class PlanError(RuntimeError):
    """Planning failed. The message is structured feedback: it names the
    variable that could not be satisfied and why, so an LLM caller can
    revise its request (different targets, lower trust floor, wider
    coverage) instead of guessing."""

    def __init__(self, msg: str, variable: str = "", chain: tuple = ()):
        self.variable = variable
        self.chain = chain
        super().__init__(msg)


@dataclass
class EventSpec:
    """Normalised trigger. Everything event-driven flows through this."""
    kind: str                          # "asteroid_impact" | ...
    lat: float
    lon: float
    time: Optional[str] = None         # ISO 8601
    radius_m: float = 50_000.0
    duration_s: float = 24 * 3600.0    # simulated window
    magnitude: dict = field(default_factory=dict)  # {"energy_mt": 5.0, ...}
    intent: str = "full_cascade"

    def bbox(self) -> BBox:
        dlat = self.radius_m / 111_000.0
        dlon = self.radius_m / (111_000.0 *
                                max(0.1, math.cos(math.radians(self.lat))))
        return (self.lon - dlon, self.lat - dlat,
                self.lon + dlon, self.lat + dlat)


@dataclass
class ComputeBudget:
    cores: int = field(default_factory=lambda: os.cpu_count() or 1)
    wall_s: float = 3600.0
    backend: str = "process"           # serial | thread | process | dask | slurm


@dataclass
class Binding:
    """One variable bound to one producer, with the audit trail."""
    variable: str
    producer: str
    kind: str                          # 'dataset' | 'model'
    score: float
    alternatives: tuple[str, ...]      # rejected candidate ids, best first
    reason: str


@dataclass
class RunPlan:
    event: EventSpec
    targets: tuple[str, ...]
    bindings: dict[str, Binding]       # variable -> binding
    producers: tuple[str, ...]         # unique bound producer ids
    resolution_m: float
    est_wall_s: float
    fits_budget: bool
    budget: ComputeBudget
    notes: tuple[str, ...] = ()

    def producer_names(self) -> tuple[str, ...]:
        return self.producers

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bindings"] = {k: asdict(v) for k, v in self.bindings.items()}
        return d


# ====================================================================
# Candidate scoring
# ====================================================================
def _score(card, magnitude: dict) -> tuple[float, str]:
    """Deterministic preference among filtered candidates.

    Fidelity dominates, then trust, then an explicit-regime-match bonus
    (a card that declares bounds containing the event beats one that is
    silent), then cheaper setup cost as the tiebreak.
    """
    reasons = []
    if isinstance(card, ModelCard):
        fid = _FIDELITY_SCORE.get(card.fidelity_tier, 2)
        reasons.append(f"fidelity={card.fidelity_tier}")
    else:
        fid = 2
    trust = trust_rank(card.quality.trust_tier)
    reasons.append(f"trust={card.quality.trust_tier}")
    regime_bonus = 0
    if isinstance(card, ModelCard) and magnitude:
        declared = set(card.valid_regimes) & set(magnitude)
        if declared:
            regime_bonus = 1
            reasons.append(f"regime-match on {sorted(declared)}")
    cost_tiebreak = 1.0 / (1.0 + card.cost.setup_s)
    score = fid * 10 + trust * 3 + regime_bonus + cost_tiebreak * 0.1
    return score, ", ".join(reasons)


def _card_id(card) -> str:
    return card.id if isinstance(card, DatasetCard) else card.name


# ====================================================================
# Planning
# ====================================================================
def plan(event: EventSpec, metacat: MetaCatalog, *,
         targets: Optional[tuple[str, ...]] = None,
         budget: Optional[ComputeBudget] = None,
         min_trust: str = "unverified",
         exclude: frozenset[str] | set[str] = frozenset()) -> RunPlan:
    """`exclude` bans specific producer ids — the critic's replan lever:
    a producer whose output failed verification is excluded and the
    next-best candidate binds instead."""
    budget = budget or ComputeBudget()
    targets = tuple(targets or targets_for_intent(event.intent))
    bbox = event.bbox()
    exclude = frozenset(exclude)

    bindings: dict[str, Binding] = {}
    bound_cards: dict[str, object] = {}     # producer id -> card
    notes: list[str] = []

    def bind(var: str, chain: tuple[str, ...]) -> None:
        if var in bindings:
            return
        if var in chain:
            raise PlanError(
                f"dependency cycle at {var!r}: {' -> '.join(chain + (var,))}",
                variable=var, chain=chain)
        cands = metacat.candidates_for(
            var, bbox=bbox, t_start=event.time, t_end=event.time,
            magnitude=event.magnitude, min_trust=min_trust)
        cands = [c for c in cands if _card_id(c) not in exclude]
        if not cands:
            unfiltered = metacat.candidates_for(var)
            hint = (f"{len(unfiltered)} producer(s) exist for {var!r} but "
                    f"none survive coverage/regime/trust/exclusion filters"
                    if unfiltered else
                    f"no registered producer for {var!r}")
            raise PlanError(
                f"cannot satisfy {var!r} (needed via "
                f"{' -> '.join(chain) or 'targets'}): {hint}",
                variable=var, chain=chain)
        scored = sorted(((*_score(c, event.magnitude), c) for c in cands),
                        key=lambda t: -t[0])
        best_score, reason, best = scored[0]
        alts = tuple(_card_id(c) for _, _, c in scored[1:])
        bindings[var] = Binding(variable=var, producer=_card_id(best),
                                kind="model" if isinstance(best, ModelCard)
                                else "dataset",
                                score=round(best_score, 3),
                                alternatives=alts, reason=reason)
        bound_cards[_card_id(best)] = best
        if isinstance(best, ModelCard):
            for req in best.requires:
                bind(req, chain + (var,))

    for t in targets:
        bind(t, ())

    resolution_m, est_wall_s, fits = _solve_resolution(
        bound_cards.values(), event, budget, notes)

    # Stable producer order: dependency-ish (order bindings were made).
    producers = tuple(dict.fromkeys(b.producer for b in bindings.values()))
    return RunPlan(event=event, targets=targets, bindings=bindings,
                   producers=producers, resolution_m=resolution_m,
                   est_wall_s=round(est_wall_s, 1), fits_budget=fits,
                   budget=budget, notes=tuple(notes))


def _solve_resolution(cards, event: EventSpec, budget: ComputeBudget,
                      notes: list[str]) -> tuple[float, float, bool]:
    """Finest ladder resolution whose estimated wall time fits the budget,
    within every bound model's declared valid resolution range."""
    finest = max((c.valid_res_m[0] for c in cards
                  if isinstance(c, ModelCard) and c.valid_res_m[0] > 0),
                 default=0.0)
    coarsest = min((c.valid_res_m[1] for c in cards
                    if isinstance(c, ModelCard) and c.valid_res_m[1] > 0),
                   default=float("inf"))
    ladder = [r for r in RESOLUTION_LADDER_M if finest <= r <= coarsest]
    if not ladder:
        raise PlanError(
            f"bound models have incompatible resolution ranges: finest "
            f"allowed {finest} m, coarsest allowed {coarsest} m")

    side_m = 2.0 * event.radius_m
    est = 0.0
    for res in ladder:                       # finest first
        cells = (side_m / res) ** 2
        steps = event.duration_s / max(DT_S_PER_RES_M * res, 1e-9)
        est = 0.0
        for c in cards:
            if isinstance(c, ModelCard):
                est += c.cost.estimate_s(cells, steps, budget.cores)
            else:                            # dataset fetch: no timesteps
                est += c.cost.estimate_s(cells, 1.0, budget.cores)
        if est <= budget.wall_s:
            notes.append(f"resolution {res} m: est {est:.0f}s fits "
                         f"budget {budget.wall_s:.0f}s on "
                         f"{budget.cores} cores")
            return res, est, True

    res = ladder[-1]
    notes.append(f"no ladder resolution fits budget {budget.wall_s:.0f}s; "
                 f"falling back to coarsest {res} m "
                 f"(est {est:.0f}s, over budget)")
    return res, est, False
