from __future__ import annotations

import dataclasses

import pytest

from capabilities import BindingParameterization, CapabilityCatalog, CapabilitySpec
from contracts import RequirementUse
from resolution.hypergraph import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitRecord,
    ArtifactCommitStatus,
    BackReferenceKind,
    DiscoveryLimitCode,
    DiscoveryLimits,
    FeasibleDerivationHypergraph,
    ProducerKind,
    build_feasible_hypergraph,
)
from stage2.demo import (
    _add_spec,
    _constant_spec,
    _deployment_snapshot,
    _descriptor,
    _requirement,
    build_demo_plan,
)


def _demo_graph(*, committed: bool = True,
                limits: DiscoveryLimits = DiscoveryLimits()):
    demo = build_demo_plan()
    status = (ArtifactCommitStatus.COMMITTED
              if committed else ArtifactCommitStatus.UNAVAILABLE)
    availability = ArtifactAvailabilitySnapshot.freeze((
        ArtifactCommitRecord(demo.offered_leaf.leaf_id, status),
    ))
    graph = build_feasible_hypergraph(
        demo.catalog,
        demo.deployment_snapshot,
        (demo.root_use,),
        artifact_leaves=(demo.offered_leaf,),
        availability_snapshot=availability,
        limits=limits,
    )
    return demo, graph


def test_recursive_discovery_builds_deterministic_packed_graph() -> None:
    demo, graph = _demo_graph()
    _, second = _demo_graph()

    assert graph.discovery_complete
    assert graph.graph_id == second.graph_id
    assert len(graph.requirement_nodes) == 3
    assert len(graph.use_nodes) == 3
    assert len(graph.invocation_nodes) == 4
    assert len(graph.artifact_nodes) == 1
    assert len(graph.satisfaction_arcs) == 6
    assert graph.candidate_count == 6
    assert not graph.rejections
    assert {node.invocation.capability_id for node in graph.invocation_nodes} == {
        "synthetic-add",
        "synthetic-left-constant",
        "synthetic-right-constant",
        "synthetic-pair",
    }
    assert any(
        arc.producer_kind is ProducerKind.ARTIFACT
        and arc.producer_id == demo.offered_leaf.leaf_id
        for arc in graph.satisfaction_arcs)
    assert FeasibleDerivationHypergraph.from_dict(graph.to_dict()) == graph


def test_equal_requirements_are_interned_without_collapsing_root_uses() -> None:
    demo = build_demo_plan()
    roots = (
        RequirementUse("root-a", "result-a", demo.root_use.requirement),
        RequirementUse("root-b", "result-b", demo.root_use.requirement),
    )
    availability = ArtifactAvailabilitySnapshot.freeze((
        ArtifactCommitRecord(
            demo.offered_leaf.leaf_id, ArtifactCommitStatus.COMMITTED),
    ))
    graph = build_feasible_hypergraph(
        demo.catalog,
        demo.deployment_snapshot,
        roots,
        artifact_leaves=(demo.offered_leaf,),
        availability_snapshot=availability,
    )

    root_requirement = next(
        node for node in graph.requirement_nodes
        if node.requirement_id == demo.root_use.requirement.requirement_id)
    assert root_requirement.use_ids == tuple(sorted(
        root.requirement_use_id for root in roots))
    assert len(graph.requirement_nodes) == 3
    assert len(graph.use_nodes) == 4
    assert len(graph.invocation_nodes) == 4
    assert len(graph.satisfaction_arcs) == 7


def test_artifact_declaration_requires_separate_trusted_commit_state() -> None:
    demo = build_demo_plan()
    with pytest.raises(ValueError, match="trusted availability"):
        build_feasible_hypergraph(
            demo.catalog,
            demo.deployment_snapshot,
            (demo.root_use,),
            artifact_leaves=(demo.offered_leaf,),
        )

    _, graph = _demo_graph(committed=False)
    assert not any(
        arc.producer_kind is ProducerKind.ARTIFACT
        for arc in graph.satisfaction_arcs)
    rejection = next(
        item for item in graph.rejections
        if item.candidate_id == demo.offered_leaf.leaf_id)
    assert rejection.codes == ("ARTIFACT_NOT_COMMITTED",)

    availability = ArtifactAvailabilitySnapshot.freeze((
        ArtifactCommitRecord(
            demo.offered_leaf.leaf_id, ArtifactCommitStatus.UNAVAILABLE),
    ))
    with pytest.raises(TypeError):
        availability._status_by_leaf[demo.offered_leaf.leaf_id] = (
            ArtifactCommitStatus.COMMITTED)
    assert (availability.status_for(demo.offered_leaf.leaf_id)
            is ArtifactCommitStatus.UNAVAILABLE)


def test_depth_and_arc_limits_are_visible_and_make_discovery_incomplete() -> None:
    demo = build_demo_plan()
    depth_limited = build_feasible_hypergraph(
        demo.catalog,
        demo.deployment_snapshot,
        (demo.root_use,),
        limits=DiscoveryLimits(max_depth=1),
    )
    assert not depth_limited.discovery_complete
    assert {reason.code for reason in depth_limited.limit_reasons} == {
        DiscoveryLimitCode.MAX_DEPTH,
    }
    assert len(depth_limited.invocation_nodes) == 1

    _, arc_limited = _demo_graph(limits=DiscoveryLimits(max_arcs=2))
    assert not arc_limited.discovery_complete
    assert len(arc_limited.satisfaction_arcs) == 2
    assert DiscoveryLimitCode.MAX_ARCS in {
        reason.code for reason in arc_limited.limit_reasons}


def test_depth_memoization_reexpands_a_shared_requirement_reached_shallower(
        ) -> None:
    top_requirement = _requirement("synthetic.depth.top")
    shared_requirement = _requirement("synthetic.depth.shared")
    leaf_requirement = _requirement("synthetic.depth.leaf")
    top_aux_requirement = _requirement("synthetic.depth.top-aux")
    shared_aux_requirement = _requirement("synthetic.depth.shared-aux")

    top_spec, top_profile = _add_spec(
        shared_requirement,
        top_aux_requirement,
        _descriptor("synthetic.depth.top"),
    )
    shared_spec, shared_profile = _add_spec(
        leaf_requirement,
        shared_aux_requirement,
        _descriptor("synthetic.depth.shared"),
    )
    leaf_spec, leaf_profile = _constant_spec(
        "depth-leaf", _descriptor("synthetic.depth.leaf"), 1)
    top_aux_spec, top_aux_profile = _constant_spec(
        "depth-top-aux", _descriptor("synthetic.depth.top-aux"), 2)
    shared_aux_spec, shared_aux_profile = _constant_spec(
        "depth-shared-aux", _descriptor("synthetic.depth.shared-aux"), 3)
    profiles = {
        value.profile_id: value for value in (
            top_profile,
            shared_profile,
            leaf_profile,
            top_aux_profile,
            shared_aux_profile,
        )
    }
    catalog = CapabilityCatalog.freeze(
        (
            top_spec,
            shared_spec,
            leaf_spec,
            top_aux_spec,
            shared_aux_spec,
        ),
        profiles.values(),
    )
    deployment = _deployment_snapshot(catalog)
    deep_root = RequirementUse(
        "depth-first-root", "result", top_requirement)
    shallow_root = RequirementUse(
        "shallow-root", "result", shared_requirement)
    assert deep_root.requirement_use_id < shallow_root.requirement_use_id

    graph = build_feasible_hypergraph(
        catalog,
        deployment,
        (deep_root, shallow_root),
        limits=DiscoveryLimits(max_depth=2),
    )
    reordered = build_feasible_hypergraph(
        catalog,
        deployment,
        (shallow_root, deep_root),
        limits=DiscoveryLimits(max_depth=2),
    )

    assert graph.discovery_complete
    assert not graph.limit_reasons
    assert graph.graph_id == reordered.graph_id
    assert len(graph.invocation_nodes) == 5
    assert {
        node.invocation.capability_id for node in graph.invocation_nodes
    } >= {"depth-leaf", "depth-top-aux", "depth-shared-aux"}


def test_candidate_limit_is_only_reported_when_an_alternative_is_omitted() -> None:
    _, exact = _demo_graph(limits=DiscoveryLimits(max_candidates=6))
    assert exact.discovery_complete
    assert exact.candidate_count == 6

    _, truncated = _demo_graph(limits=DiscoveryLimits(max_candidates=5))
    assert not truncated.discovery_complete
    assert truncated.candidate_count == 5
    assert DiscoveryLimitCode.MAX_CANDIDATES in {
        reason.code for reason in truncated.limit_reasons}


def test_requirement_and_invocation_limits_preserve_a_valid_partial_graph() -> None:
    demo = build_demo_plan()
    requirement_limited = build_feasible_hypergraph(
        demo.catalog,
        demo.deployment_snapshot,
        (demo.root_use,),
        limits=DiscoveryLimits(max_requirements=2),
    )
    assert not requirement_limited.discovery_complete
    assert len(requirement_limited.requirement_nodes) == 1
    assert not requirement_limited.invocation_nodes
    assert DiscoveryLimitCode.MAX_REQUIREMENTS in {
        reason.code for reason in requirement_limited.limit_reasons}

    invocation_limited = build_feasible_hypergraph(
        demo.catalog,
        demo.deployment_snapshot,
        (demo.root_use,),
        limits=DiscoveryLimits(max_invocations=1),
    )
    assert not invocation_limited.discovery_complete
    assert len(invocation_limited.invocation_nodes) == 1
    assert DiscoveryLimitCode.MAX_INVOCATIONS in {
        reason.code for reason in invocation_limited.limit_reasons}


def test_recursive_cycle_is_retained_as_a_typed_back_reference() -> None:
    recursive_descriptor = _descriptor("synthetic.recursive.value")
    recursive_requirement = _requirement("synthetic.recursive.value")
    base_descriptor = _descriptor("synthetic.base.value")
    base_requirement = _requirement("synthetic.base.value")
    recursive_spec, recursive_profile = _add_spec(
        recursive_requirement, base_requirement, recursive_descriptor)
    base_spec, base_profile = _constant_spec(
        "synthetic-base-constant", base_descriptor, 1)
    catalog = CapabilityCatalog.freeze(
        (recursive_spec, base_spec), (recursive_profile, base_profile))
    deployment = _deployment_snapshot(catalog)
    root = RequirementUse("recursive-root", "result", recursive_requirement)

    graph = build_feasible_hypergraph(catalog, deployment, (root,))

    assert graph.discovery_complete
    assert any(
        reference.kind is BackReferenceKind.CYCLE
        and reference.requirement_id == recursive_requirement.requirement_id
        for reference in graph.back_references)
    assert len(graph.requirement_nodes) == 2
    assert len(graph.invocation_nodes) == 2


@pytest.mark.parametrize(
    ("cost_model_id", "metrics", "expected_code"),
    (
        ("cost:unsupported-v9", {"cost_units": 1},
         "COST_MODEL_UNSUPPORTED"),
        ("cost:declared-v1", {}, "COST_ESTIMATE_INVALID"),
        ("cost:declared-v1", {"cost_units": 1 << 40},
         "COST_ESTIMATE_INVALID"),
    ),
)
def test_unsupported_cost_records_are_structured_discovery_rejections(
        cost_model_id, metrics, expected_code):
    descriptor = _descriptor("synthetic.cost.value")
    requirement = _requirement("synthetic.cost.value")
    base, profile = _constant_spec("invalid-cost", descriptor, 1)
    spec = CapabilitySpec.bind(
        capability_id=base.capability_id,
        capability_version=base.capability_version,
        implementation=base.implementation,
        binder=base.binder,
        input_ports=base.input_ports,
        output_ports=base.output_ports,
        parameter_schema=base.parameter_schema,
        parameterizations=(BindingParameterization(
            {"value": 1}, metrics),),
        execution_profile_id=base.execution_profile_id,
        cost_model_id=cost_model_id,
    )
    catalog = CapabilityCatalog.freeze((spec,), (profile,))
    graph = build_feasible_hypergraph(
        catalog,
        _deployment_snapshot(catalog),
        (RequirementUse("invalid-cost-root", "result", requirement),),
    )

    assert graph.discovery_complete
    assert not graph.invocation_nodes
    assert not graph.satisfaction_arcs
    assert any(expected_code in value.codes for value in graph.rejections)


def test_incomplete_binding_enumeration_marks_discovery_incomplete(monkeypatch):
    demo = build_demo_plan()
    original = CapabilityCatalog.bind_candidates

    def incomplete(self, *args, **kwargs):
        return dataclasses.replace(
            original(self, *args, **kwargs), complete=False)

    monkeypatch.setattr(CapabilityCatalog, "bind_candidates", incomplete)
    graph = build_feasible_hypergraph(
        demo.catalog, demo.deployment_snapshot, (demo.root_use,))

    assert not graph.discovery_complete
    assert DiscoveryLimitCode.BINDING_ENUMERATION_INCOMPLETE in {
        value.code for value in graph.limit_reasons}


def test_named_but_unbound_evidence_profile_is_rejected():
    descriptor = _descriptor("synthetic.evidence.value")
    requirement = _requirement("synthetic.evidence.value")
    base, profile = _constant_spec("missing-evidence", descriptor, 1)
    spec = CapabilitySpec.bind(
        capability_id=base.capability_id,
        capability_version=base.capability_version,
        implementation=base.implementation,
        binder=base.binder,
        input_ports=base.input_ports,
        output_ports=base.output_ports,
        parameter_schema=base.parameter_schema,
        parameterizations=base.parameterizations,
        execution_profile_id=base.execution_profile_id,
        evidence_profile_id="evidence:missing-profile",
    )
    catalog = CapabilityCatalog.freeze((spec,), (profile,))
    graph = build_feasible_hypergraph(
        catalog,
        _deployment_snapshot(catalog),
        (RequirementUse("missing-evidence-root", "result", requirement),),
    )

    assert not graph.satisfaction_arcs
    assert any(
        "EVIDENCE_PROFILE_UNBOUND" in value.codes
        for value in graph.rejections)
