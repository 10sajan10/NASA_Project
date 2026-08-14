"""Lossless projection from recursive discovery to the selector graph.

The Stage-2 exhaustive oracle and Stage-3 MILP intentionally consume the same
compact :class:`OracleProblem`.  Keeping this adapter small and typed prevents
the production selector and correctness oracle from drifting semantically.
"""
from __future__ import annotations

from composition.oracle import (
    ArtifactLeafNode,
    InvocationNode,
    OracleProblem,
    RequirementUseNode,
    SatisfactionArc,
)
from plans import PlanSnapshotRef

from .hypergraph import (
    FeasibleDerivationHypergraph,
    ProducerKind as HypergraphProducerKind,
)


def project_oracle_problem(
    graph: FeasibleDerivationHypergraph,
) -> OracleProblem:
    """Project one discovered graph without rediscovery or filtering.

    Scientific requirements, compatibility proofs, producer identities,
    cardinality and use identities are preserved exactly.  Discovery-level
    rejections remain on ``graph`` because a rejected capability is not a
    producer node and therefore cannot be represented as an OracleProblem arc.
    """
    if not isinstance(graph, FeasibleDerivationHypergraph):
        raise TypeError("graph must be FeasibleDerivationHypergraph")

    typed_uses = {value.use_id: value for value in graph.use_nodes}
    typed_invocations = {
        value.invocation_id: value.invocation for value in graph.invocation_nodes
    }
    typed_artifacts = {
        value.leaf_id: value.leaf for value in graph.artifact_nodes
    }
    deployment_by_profile = {
        value.profile_id: value for value in graph.deployment_proofs
    }

    uses = tuple(RequirementUseNode.from_requirement_use(
        value.use,
        owner_invocation_id=value.owner_invocation_id,
    ) for value in graph.use_nodes)
    invocations = tuple(InvocationNode.from_bound_invocation(
        value.invocation,
        deployment=deployment_by_profile[
            value.invocation.execution_profile_id],
    ) for value in graph.invocation_nodes)
    artifacts = tuple(ArtifactLeafNode(
        leaf_id=value.leaf_id,
        output_port_ids=("artifact",),
        cost_units=value.cost_units,
        committed=value.committed,
    ) for value in graph.artifact_nodes)

    arcs: list[SatisfactionArc] = []
    for value in graph.satisfaction_arcs:
        use = typed_uses[value.use_id].use
        if value.producer_kind is HypergraphProducerKind.INVOCATION:
            producer = typed_invocations[value.producer_id]
        else:
            producer = typed_artifacts[value.producer_id]
        arcs.append(SatisfactionArc.from_compatibility(
            use,
            producer,
            value.output_port_id,
            value.compatibility,
        ))

    snapshots = [
        PlanSnapshotRef("capability_catalog", graph.catalog_id),
        PlanSnapshotRef("deployment_feasibility", graph.deployment_snapshot_id),
        PlanSnapshotRef("feasible_hypergraph", graph.graph_id),
    ]
    if graph.availability_snapshot_id is not None:
        snapshots.append(PlanSnapshotRef(
            "artifact_availability", graph.availability_snapshot_id))
    if graph.evidence_snapshot_id is not None:
        snapshots.append(PlanSnapshotRef(
            "evidence", graph.evidence_snapshot_id))

    return OracleProblem.bind(
        f"stage3-discovered-{graph.graph_id}",
        uses=uses,
        root_use_ids=graph.root_use_ids,
        invocations=invocations,
        artifact_leaves=artifacts,
        satisfaction_arcs=arcs,
        candidate_rejections=(),
        snapshot_refs=snapshots,
    )


__all__ = ["project_oracle_problem"]
