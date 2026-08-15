"""Enumerating admissible source choices by constrained re-solve.

The MVP does not enumerate a Pareto front and does not claim non-dominance.
It does something much weaker and much more defensible: for each producer that
could supply the contested concept, it re-solves the *whole* problem with that
producer forced in, and reports whether a globally consistent plan exists and
what it costs.

That distinction matters.  A plan built by locally swapping one producer can be
invalid or suboptimal elsewhere in the graph, because producers share inputs and
co-produce outputs.  Re-solving means each presented alternative is a real,
globally consistent plan the user can actually run.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from capabilities.implementation import _required_text
from contracts import EvidenceProfile, EvidenceSnapshot, RequirementUse
from engine.runtime.identity import require_object_fields
from plans import ProducerKind
from resolution import (
    FeasibleDerivationHypergraph,
    ProducerSelectionRef,
    ResolutionOutcome,
    ResolutionStatus,
    SelectionConstraints,
)

from .comparability import MetricReading, read_metric


@dataclass(frozen=True)
class SourceAlternative:
    """One globally consistent plan in which a named producer is used."""

    producer: ProducerSelectionRef
    capability_id: str
    admissible: bool
    plan_id: str | None
    cost_units: int | None
    resolution_status: str
    reading: MetricReading | None
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.producer, ProducerSelectionRef):
            raise TypeError("alternative requires a typed producer reference")
        _required_text(self.capability_id, "alternative capability_id")
        if type(self.admissible) is not bool:
            raise TypeError("alternative admissibility must be bool")
        if self.admissible and (self.plan_id is None or self.cost_units is None):
            raise ValueError("an admissible alternative needs a plan and cost")
        if not self.admissible and not self.detail:
            raise ValueError("an inadmissible alternative must explain itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "producer": self.producer.to_dict(),
            "capability_id": self.capability_id,
            "admissible": self.admissible,
            "plan_id": self.plan_id,
            "cost_units": self.cost_units,
            "resolution_status": self.resolution_status,
            "reading": self.reading.to_dict() if self.reading else None,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceAlternative":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "SourceAlternative")
        producer = raw["producer"]
        raw["producer"] = ProducerSelectionRef(
            ProducerKind(producer["producer_kind"]), producer["producer_id"])
        raw["reading"] = (MetricReading.from_dict(raw["reading"])
                          if raw["reading"] is not None else None)
        return cls(**raw)


def candidate_producers_for_concept(
        graph: FeasibleDerivationHypergraph,
        concept_id: str) -> tuple[tuple[ProducerSelectionRef, str], ...]:
    """Find every discovered producer that can supply ``concept_id``.

    Returns ``(producer reference, capability or artifact label)`` pairs, sorted
    for determinism.  Both invocations and committed artifact leaves count: a
    scientist choosing a wind source does not care which of those it is.
    """
    _required_text(concept_id, "concept_id")
    found: list[tuple[ProducerSelectionRef, str]] = []
    for node in graph.invocation_nodes:
        if any(output.descriptor.concept_id == concept_id
               for output in node.invocation.outputs):
            found.append((
                ProducerSelectionRef(ProducerKind.INVOCATION,
                                     node.invocation_id),
                node.invocation.capability_id))
    for node in graph.artifact_nodes:
        if node.leaf.descriptor.concept_id == concept_id:
            found.append((
                ProducerSelectionRef(ProducerKind.ARTIFACT_LEAF, node.leaf_id),
                f"artifact:{node.leaf.artifact_id[:16]}"))
    return tuple(sorted(found, key=lambda item: (item[1], item[0].producer_id)))


def enumerate_source_alternatives(
    baseline: ResolutionOutcome,
    resolve: Callable[[SelectionConstraints], ResolutionOutcome],
    *,
    concept_id: str,
    requirement_use: RequirementUse,
    metric_definition_id: str,
    evidence_snapshot: EvidenceSnapshot | None = None,
    evidence_by_capability: dict[str, EvidenceProfile] | None = None,
) -> tuple[SourceAlternative, ...]:
    """Re-solve once per candidate producer and collect the real alternatives.

    ``resolve`` must apply the supplied constraints to the *same* frozen graph
    inputs the baseline used; otherwise the alternatives would not be
    comparable as plans, let alone as science.
    """
    profiles = evidence_by_capability or {}
    alternatives: list[SourceAlternative] = []
    for producer, label in candidate_producers_for_concept(
            baseline.hypergraph, concept_id):
        constrained = resolve(SelectionConstraints.bind(include=(producer,)))
        admissible = (constrained.status is ResolutionStatus.READY
                      and constrained.selection.plan is not None)
        profile = profiles.get(label)
        reading = read_metric(
            label, profile, metric_definition_id, requirement_use.requirement)
        if admissible:
            alternatives.append(SourceAlternative(
                producer=producer, capability_id=label, admissible=True,
                plan_id=constrained.selection.plan.plan_id,
                cost_units=constrained.selection.objective_cost_units,
                resolution_status=constrained.status.value,
                reading=reading))
        else:
            alternatives.append(SourceAlternative(
                producer=producer, capability_id=label, admissible=False,
                plan_id=None, cost_units=None,
                resolution_status=constrained.status.value,
                reading=reading,
                detail=(
                    "no globally consistent plan exists with this producer "
                    f"forced in ({constrained.status.value})")))
    return tuple(alternatives)


def admissible_alternatives(
        alternatives: Iterable[SourceAlternative]
) -> tuple[SourceAlternative, ...]:
    return tuple(item for item in alternatives if item.admissible)


__all__ = [
    "SourceAlternative",
    "admissible_alternatives",
    "candidate_producers_for_concept",
    "enumerate_source_alternatives",
]
