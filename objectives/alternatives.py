"""Enumerating admissible source choices by constrained re-solve.

The MVP does not enumerate a Pareto front and does not claim non-dominance.
It does something much weaker and much more defensible: for each producer
output with a compatibility arc to the contested requirement use, it re-solves
the *whole* problem with that exact arc forced in, and reports whether a
globally consistent plan exists and what it costs.

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
from capabilities import artifact_evidence_subject, invocation_evidence_subject
from contracts import EvidenceProfile, EvidenceSnapshot, RequirementUse
from engine.runtime.identity import require_object_fields
from engine.runtime.identity import strict_hash
from plans import ProducerKind
from resolution import (
    FeasibleDerivationHypergraph,
    ProducerSelectionRef,
    ResolutionOutcome,
    ResolutionStatus,
    SatisfactionArcSelectionRef,
    SelectionConstraints,
)

from .comparability import MetricReading, read_metric


@dataclass(frozen=True)
class SourceAlternative:
    """One plan where a named output satisfies the contested use."""

    producer: ProducerSelectionRef
    satisfaction: SatisfactionArcSelectionRef
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
        if not isinstance(self.satisfaction, SatisfactionArcSelectionRef):
            raise TypeError(
                "alternative requires a typed satisfaction-arc reference")
        if self.satisfaction.producer != self.producer:
            raise ValueError(
                "alternative producer and satisfaction arc must agree")
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
            "satisfaction": self.satisfaction.to_dict(),
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
        raw["producer"] = ProducerSelectionRef.from_dict(raw["producer"])
        raw["satisfaction"] = SatisfactionArcSelectionRef.from_dict(
            raw["satisfaction"])
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


def candidate_satisfactions_for_use(
    baseline: ResolutionOutcome,
    requirement_use: RequirementUse,
    concept_id: str,
) -> tuple[tuple[SatisfactionArcSelectionRef, str], ...]:
    """Return only producer outputs that can satisfy the contested use.

    Merely producing the same concept elsewhere in the graph is insufficient:
    the frozen selector graph must contain the exact compatibility arc to this
    distinct consumer-port use.  Labels come from the corresponding typed
    hypergraph node, but the constraint identity comes from the projected
    selector graph that the MILP actually solves.
    """
    if not isinstance(baseline, ResolutionOutcome):
        raise TypeError("baseline must be a ResolutionOutcome")
    if not isinstance(requirement_use, RequirementUse):
        raise TypeError("requirement_use must be a RequirementUse")
    _required_text(concept_id, "concept_id")
    if requirement_use.requirement.concept_id != concept_id:
        raise ValueError(
            "contested concept does not match the requirement-use contract")

    # Root uses already carry their final graph identity.  Capability input
    # templates, however, receive an invocation-local use ID only when bound.
    # Stage 6 historically passed a template-shaped RequirementUse for the
    # latter.  Resolve that shorthand only when its requirement+port maps to
    # exactly one frozen graph use; ambiguity must be surfaced to the caller,
    # never settled by choosing the first matching consumer.
    use_by_id = {
        node.use_id: node for node in baseline.hypergraph.use_nodes
    }
    if requirement_use.requirement_use_id in use_by_id:
        contested_use_id = requirement_use.requirement_use_id
    else:
        matching_uses = tuple(
            node for node in baseline.hypergraph.use_nodes
            if node.requirement_id
            == requirement_use.requirement.requirement_id
            and node.port_id == requirement_use.port_id)
        if not matching_uses:
            raise ValueError(
                "contested requirement use is absent from the frozen graph")
        if len(matching_uses) != 1:
            raise ValueError(
                "contested requirement template maps to multiple frozen uses; "
                "supply the exact bound RequirementUse")
        contested_use_id = matching_uses[0].use_id

    invocations = {
        node.invocation_id: node.invocation
        for node in baseline.hypergraph.invocation_nodes
    }
    artifacts = {
        node.leaf_id: node.leaf for node in baseline.hypergraph.artifact_nodes
    }
    found: list[tuple[SatisfactionArcSelectionRef, str]] = []
    for arc in baseline.selector_problem.satisfaction_arcs:
        if arc.use_id != contested_use_id:
            continue
        reference = SatisfactionArcSelectionRef.from_arc(arc)
        if arc.producer_kind is ProducerKind.INVOCATION:
            invocation = invocations[arc.producer_id]
            output = invocation.output(arc.output_port_id)
            if output.descriptor.concept_id != concept_id:
                raise ValueError(
                    "selector arc output disagrees with the contested concept")
            label = invocation.capability_id
        else:
            leaf = artifacts[arc.producer_id]
            if leaf.descriptor.concept_id != concept_id:
                raise ValueError(
                    "selector artifact disagrees with the contested concept")
            label = f"artifact:{leaf.artifact_id[:16]}"
        found.append((reference, label))
    return tuple(sorted(
        found,
        key=lambda item: (
            item[1], item[0].producer_kind.value, item[0].producer_id,
            item[0].output_port_id),
    ))


def resolve_profile_from_snapshot(
    graph: FeasibleDerivationHypergraph,
    snapshot: EvidenceSnapshot | None,
    producer: ProducerSelectionRef,
    concept_id: str,
    output_port_id: str | None = None,
) -> EvidenceProfile | None:
    """Find the evidence profile the *frozen snapshot* binds to this producer.

    The evidence subject is derived from the producer itself and then matched
    against the snapshot, exactly as ``direct_match`` does it.  Nothing is
    taken on trust from a caller-supplied mapping: a profile that is not in the
    frozen snapshot, or whose subject this producer could not have produced,
    simply does not exist as far as the decision report is concerned.
    """
    if snapshot is None:
        return None
    if producer.producer_kind is ProducerKind.INVOCATION:
        node = next((item for item in graph.invocation_nodes
                     if item.invocation_id == producer.producer_id), None)
        if node is None:
            return None
        port = next((
            item for item in node.invocation.outputs
            if item.descriptor.concept_id == concept_id
            and (output_port_id is None or item.port_id == output_port_id)
        ), None)
        if port is None:
            return None
        subject = invocation_evidence_subject(node.invocation, port.port_id)
    else:
        node = next((item for item in graph.artifact_nodes
                     if item.leaf_id == producer.producer_id), None)
        if node is None:
            return None
        subject = artifact_evidence_subject(node.leaf)
    return next((item for item in snapshot.profiles
                 if item.subject == subject), None)


def enumerate_source_alternatives(
    baseline: ResolutionOutcome,
    resolve: Callable[[SelectionConstraints], ResolutionOutcome],
    *,
    concept_id: str,
    requirement_use: RequirementUse,
    metric_definition_id: str,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> tuple[SourceAlternative, ...]:
    """Re-solve once per candidate producer and collect the real alternatives.

    ``resolve`` must apply the supplied constraints to the *same* frozen graph
    inputs the baseline used; otherwise the alternatives would not be
    comparable as plans, let alone as science.  Evidence is resolved from the
    frozen snapshot rather than accepted from the caller.
    """
    expected_evidence_id = baseline.hypergraph.evidence_snapshot_id
    observed_evidence_id = (
        evidence_snapshot.snapshot_id if evidence_snapshot is not None else None)
    if observed_evidence_id != expected_evidence_id:
        raise ValueError(
            "decision evidence snapshot differs from the frozen resolution "
            "universe")
    frozen_context = resolution_context_id(baseline)
    alternatives: list[SourceAlternative] = []
    for satisfaction, label in candidate_satisfactions_for_use(
            baseline, requirement_use, concept_id):
        producer = satisfaction.producer
        constrained = resolve(SelectionConstraints.bind(
            required_satisfactions=(satisfaction,)))
        if resolution_context_id(constrained) != frozen_context:
            raise ValueError(
                "constrained alternative was resolved in another frozen "
                "planning universe")
        plan = constrained.selection.plan
        satisfaction_selected = (
            plan is not None
            and any(
                binding.use_id == satisfaction.use_id
                and any(
                    output.producer_kind is satisfaction.producer_kind
                    and output.producer_id == satisfaction.producer_id
                    and output.output_port_id == satisfaction.output_port_id
                    for output in binding.outputs)
                for binding in plan.satisfactions))
        admissible = (constrained.status is ResolutionStatus.READY
                      and satisfaction_selected)
        profile = resolve_profile_from_snapshot(
            baseline.hypergraph, evidence_snapshot, producer, concept_id,
            satisfaction.output_port_id)
        reading = read_metric(
            label, profile, metric_definition_id, requirement_use.requirement)
        if admissible:
            alternatives.append(SourceAlternative(
                producer=producer, satisfaction=satisfaction,
                capability_id=label, admissible=True,
                plan_id=constrained.selection.plan.plan_id,
                cost_units=constrained.selection.objective_cost_units,
                resolution_status=constrained.status.value,
                reading=reading))
        else:
            detail = (
                "resolver returned a plan that did not honor the exact "
                "contested satisfaction constraint"
                if (constrained.status is ResolutionStatus.READY
                    and plan is not None)
                else (
                    "no globally consistent plan exists with this exact "
                    "producer-output forced to satisfy the contested use "
                    f"({constrained.status.value})"))
            alternatives.append(SourceAlternative(
                producer=producer, satisfaction=satisfaction,
                capability_id=label, admissible=False,
                plan_id=None, cost_units=None,
                resolution_status=constrained.status.value,
                reading=reading,
                detail=detail))
    return tuple(alternatives)


def resolution_context_id(outcome: ResolutionOutcome) -> str:
    """Identity of facts that must remain fixed across constrained re-solves."""
    if not isinstance(outcome, ResolutionOutcome):
        raise TypeError("resolution context requires ResolutionOutcome")
    return strict_hash({
        "schema": "stage8r-objective-resolution-context-v1",
        "hypergraph_id": outcome.hypergraph.graph_id,
        "catalog_id": outcome.hypergraph.catalog_id,
        "deployment_snapshot_id": outcome.hypergraph.deployment_snapshot_id,
        "availability_snapshot_id": outcome.hypergraph.availability_snapshot_id,
        "evidence_snapshot_id": outcome.hypergraph.evidence_snapshot_id,
        "selector_graph_id": outcome.selector_problem.problem_id,
        "selector_snapshot_refs": [
            item.to_dict() for item in outcome.selector_problem.snapshot_refs],
        "discovery_certificate_id":
            outcome.discovery_certificate.certificate_id,
        "discovery_universe_id": outcome.discovery_universe.universe_id,
    })


def admissible_alternatives(
        alternatives: Iterable[SourceAlternative]
) -> tuple[SourceAlternative, ...]:
    return tuple(item for item in alternatives if item.admissible)


__all__ = [
    "SourceAlternative",
    "resolve_profile_from_snapshot",
    "admissible_alternatives",
    "candidate_producers_for_concept",
    "candidate_satisfactions_for_use",
    "enumerate_source_alternatives",
    "resolution_context_id",
]
