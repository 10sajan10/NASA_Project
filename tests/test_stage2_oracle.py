from __future__ import annotations

import copy

import pytest

from composition import (
    ArtifactLeafNode,
    CandidateRejection,
    CardinalityRange,
    DistinctBy,
    InvocationNode,
    OracleProblem,
    OracleStatus,
    RequirementUseNode,
    SatisfactionArc,
    exhaustive_enumerate,
)
from capabilities import CapabilityCatalog
from contracts import (
    CheckStatus,
    CompatibilityCheck,
    CompatibilityProof,
    MatchCode,
    RequirementUse,
)
from engine.runtime.identity import strict_copy
from plans import (
    ArtifactLeafBinding,
    BoundInvocationBinding,
    BoundDerivationPlan,
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
    ProducerKind,
    SatisfactionKind,
)


def _proof(use_id: str, producer_id: str, port: str) -> CompatibilityProofRecord:
    proof = CompatibilityProof(
        requirement_id=f"requirement:{use_id}",
        descriptor_id=f"descriptor:{producer_id}:{port}",
        evidence_profile_id=None,
        evidence_snapshot_id=None,
        evidence_subject_id=None,
        checks=(CompatibilityCheck(
            dimension="synthetic",
            status=CheckStatus.PASS,
            code=MatchCode.MATCH,
            detail="closed oracle fixture",
        ),),
    )
    return CompatibilityProofRecord.from_compatibility(proof)


def _arc(use_id: str, producer_id: str, *, port: str = "result",
         kind: ProducerKind = ProducerKind.INVOCATION) -> SatisfactionArc:
    return SatisfactionArc(
        use_id, producer_id, kind, port,
        _proof(use_id, producer_id, port),
    )


def _source(name: str, cost: int, *ports: str) -> InvocationNode:
    return InvocationNode(
        invocation_id=name,
        input_use_ids=(),
        output_port_ids=tuple(sorted(ports or ("result",))),
        cost_units=cost,
        zero_input_source=True,
    )


def _codes(node: object) -> list[str]:
    result: list[str] = []

    def walk(value: object) -> None:
        result.append(value.code)  # type: ignore[attr-defined]
        for child in value.children:  # type: ignore[attr-defined]
            walk(child)

    walk(node)
    return result


def _nodes(node: object) -> list[object]:
    result: list[object] = []

    def walk(value: object) -> None:
        result.append(value)
        for child in value.children:  # type: ignore[attr-defined]
            walk(child)

    walk(node)
    return result


def test_direct_alternatives_minimum_cost_and_tie_order_are_deterministic() -> None:
    root = RequirementUseNode("root", "same-requirement", "result")
    a = _source("a-source", 1, "result")
    b = _source("b-source", 1, "result")
    arcs = (_arc("root", "a-source"), _arc("root", "b-source"))
    problem = OracleProblem.bind(
        "direct-alternatives",
        uses=(root,), root_use_ids=(root.use_id,),
        invocations=(b, a), satisfaction_arcs=reversed(arcs),
        snapshot_refs=(PlanSnapshotRef("catalog", "catalog:v1"),),
    )

    first = exhaustive_enumerate(problem)
    second = exhaustive_enumerate(problem)

    assert first.status is OracleStatus.OPTIMAL
    assert first.complete
    assert len(first.plans) == 2
    assert first.optimal_plan.plan_id == second.optimal_plan.plan_id
    # The versioned 0-before-1 producer bit vector deliberately selects the
    # lexicographically later producer in a cost tie.
    assert first.optimal_plan.selected_invocation_ids == ("b-source",)
    assert first.optimal_plan.total_cost_units == 1


def test_multi_output_co_producer_is_selected_and_costed_once() -> None:
    # Equal normalized requirement values remain two distinct root uses.
    left = RequirementUseNode("left-use", "shared-requirement", "left")
    right = RequirementUseNode("right-use", "shared-requirement", "right")
    pair = _source("pair", 3, "left", "right")
    left_only = _source("left-only", 2, "result")
    right_only = _source("right-only", 2, "result")
    problem = OracleProblem.bind(
        "co-production",
        uses=(left, right), root_use_ids=(left.use_id, right.use_id),
        invocations=(pair, left_only, right_only),
        satisfaction_arcs=(
            _arc(left.use_id, pair.invocation_id, port="left"),
            _arc(right.use_id, pair.invocation_id, port="right"),
            _arc(left.use_id, left_only.invocation_id),
            _arc(right.use_id, right_only.invocation_id),
        ),
    )

    result = exhaustive_enumerate(problem)

    assert result.optimal_plan.selected_invocation_ids == ("pair",)
    assert result.optimal_plan.total_cost_units == 3
    assert {item.use_id for item in result.optimal_plan.satisfactions} == {
        "left-use", "right-use"}
    assert sum(
        output.producer_id == "pair"
        for item in result.optimal_plan.satisfactions for output in item.outputs
    ) == 2


def test_optional_use_enumerates_omit_default_and_producer_exclusively() -> None:
    root = RequirementUseNode("root", "final", "result")
    optional = RequirementUseNode(
        "consumer-optional", "optional-value", "maybe",
        owner_invocation_id="consumer", optional=True,
        default_id="default:zero", cardinality=CardinalityRange(0, 1),
    )
    consumer = InvocationNode(
        "consumer", (optional.use_id,), ("result",), 1)
    producer = _source("optional-producer", 0, "result")
    problem = OracleProblem.bind(
        "optional-default",
        uses=(root, optional), root_use_ids=(root.use_id,),
        invocations=(consumer, producer),
        satisfaction_arcs=(
            _arc(root.use_id, consumer.invocation_id),
            _arc(optional.use_id, producer.invocation_id),
        ),
    )

    result = exhaustive_enumerate(problem)
    optional_kinds = {
        next(value for value in plan.satisfactions
             if value.use_id == optional.use_id).kind
        for plan in result.plans
    }

    assert optional_kinds == {
        SatisfactionKind.PRODUCERS,
        SatisfactionKind.DEFAULT,
        SatisfactionKind.OMIT,
    }
    for plan in result.plans:
        binding = next(value for value in plan.satisfactions
                       if value.use_id == optional.use_id)
        assert (bool(binding.outputs) + (binding.default_id is not None)
                + (binding.kind is SatisfactionKind.OMIT)) == 1


def test_cardinality_and_distinct_producer_filter_same_invocation_outputs() -> None:
    root = RequirementUseNode(
        "ensemble", "ensemble-member", "members",
        cardinality=CardinalityRange(2, 2),
        distinct_by=DistinctBy.PRODUCER,
    )
    pair = _source("pair", 0, "member-a", "member-b")
    other = _source("other", 1, "result")
    problem = OracleProblem.bind(
        "cardinality",
        uses=(root,), root_use_ids=(root.use_id,),
        invocations=(pair, other),
        satisfaction_arcs=(
            _arc(root.use_id, pair.invocation_id, port="member-a"),
            _arc(root.use_id, pair.invocation_id, port="member-b"),
            _arc(root.use_id, other.invocation_id),
        ),
    )

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.OPTIMAL
    assert result.plans
    for plan in result.plans:
        outputs = plan.satisfactions[0].outputs
        assert len(outputs) == 2
        assert len({value.producer_id for value in outputs}) == 2


def test_shareable_output_can_fill_two_uses_but_group_distinctness_forbids_it() -> None:
    leaf = ArtifactLeafNode("leaf", ("artifact",), committed=True)
    one = RequirementUseNode(
        "position-1", "same-observation", "first",
        distinctness_group="pair", shareable=True)
    two = RequirementUseNode(
        "position-2", "same-observation", "second",
        distinctness_group="pair", shareable=True)
    arcs = (
        _arc(one.use_id, leaf.leaf_id, port="artifact",
             kind=ProducerKind.ARTIFACT_LEAF),
        _arc(two.use_id, leaf.leaf_id, port="artifact",
             kind=ProducerKind.ARTIFACT_LEAF),
    )
    distinct_problem = OracleProblem.bind(
        "paired-distinctness", uses=(one, two),
        root_use_ids=(one.use_id, two.use_id), artifact_leaves=(leaf,),
        satisfaction_arcs=arcs)

    distinct_result = exhaustive_enumerate(distinct_problem)
    assert distinct_result.status is OracleStatus.UNSATISFIABLE
    assert "DISTINCTNESS_CONFLICT" in _codes(distinct_result.blocker_tree.root)

    share_one = dataclasses_replace(one, distinctness_group=None)
    share_two = dataclasses_replace(two, distinctness_group=None)
    shared_problem = OracleProblem.bind(
        "shareable", uses=(share_one, share_two),
        root_use_ids=(share_one.use_id, share_two.use_id),
        artifact_leaves=(leaf,), satisfaction_arcs=arcs)
    shared_result = exhaustive_enumerate(shared_problem)
    assert shared_result.status is OracleStatus.OPTIMAL
    assert shared_result.optimal_plan.selected_artifact_leaf_ids == ("leaf",)
    assert shared_result.optimal_plan.total_cost_units == 0


def dataclasses_replace(value: object, **changes: object) -> object:
    # A tiny local wrapper keeps the fixture's intent visible without importing
    # mutable builder machinery into the production oracle.
    import dataclasses
    return dataclasses.replace(value, **changes)


def test_nonshareable_output_reuse_is_rejected() -> None:
    leaf = ArtifactLeafNode("leaf", ("artifact",), committed=True)
    one = RequirementUseNode("one", "same", "one", shareable=False)
    two = RequirementUseNode("two", "same", "two")
    problem = OracleProblem.bind(
        "nonshareable", uses=(one, two), root_use_ids=("one", "two"),
        artifact_leaves=(leaf,), satisfaction_arcs=(
            _arc("one", "leaf", port="artifact",
                 kind=ProducerKind.ARTIFACT_LEAF),
            _arc("two", "leaf", port="artifact",
                 kind=ProducerKind.ARTIFACT_LEAF),
        ))

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.UNSATISFIABLE
    assert "NON_SHAREABLE_OUTPUT_REUSED" in _codes(result.blocker_tree.root)


def test_selected_cycle_is_forbidden_but_atomic_iterative_source_is_one_node() -> None:
    root = RequirementUseNode("root", "result", "result")
    a_input = RequirementUseNode(
        "a-input", "feedback", "input", owner_invocation_id="a")
    b_input = RequirementUseNode(
        "b-input", "feedback", "input", owner_invocation_id="b")
    a = InvocationNode("a", (a_input.use_id,), ("result",), 1)
    b = InvocationNode("b", (b_input.use_id,), ("result",), 1)
    cyclic = OracleProblem.bind(
        "pure-cycle", uses=(root, a_input, b_input),
        root_use_ids=(root.use_id,), invocations=(a, b),
        satisfaction_arcs=(
            _arc(root.use_id, "a"),
            _arc(a_input.use_id, "b"),
            _arc(b_input.use_id, "a"),
        ))

    rejected = exhaustive_enumerate(cyclic)
    assert rejected.status is OracleStatus.UNSATISFIABLE
    assert "SELECTED_CYCLE" in _codes(rejected.blocker_tree.root)

    atomic = InvocationNode(
        "atomic-fixed-point", (), ("result",), 2,
        zero_input_source=True, atomic_iterative=True)
    accepted_problem = OracleProblem.bind(
        "atomic-iteration", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(atomic,), satisfaction_arcs=(
            _arc(root.use_id, atomic.invocation_id),))
    accepted = exhaustive_enumerate(accepted_problem)
    assert accepted.status is OracleStatus.OPTIMAL
    assert accepted.optimal_plan.selected_invocation_ids == (
        "atomic-fixed-point",)


def test_zero_input_invocation_requires_explicit_source_grounding() -> None:
    root = RequirementUseNode("root", "result", "result")
    ungrounded = InvocationNode("mystery", (), ("result",), 0)
    problem = OracleProblem.bind(
        "ungrounded", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(ungrounded,), satisfaction_arcs=(
            _arc(root.use_id, ungrounded.invocation_id),))

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.UNSATISFIABLE
    assert "UNGROUNDED_INVOCATION" in _codes(result.blocker_tree.root)


def test_missing_inputs_report_every_producer_branch_and_match_rejection() -> None:
    root = RequirementUseNode("root", "final", "result")
    a_input = RequirementUseNode(
        "a-input", "wind", "wind", owner_invocation_id="a")
    b_input = RequirementUseNode(
        "b-input", "wind", "wind", owner_invocation_id="b")
    a = InvocationNode("a", (a_input.use_id,), ("result",), 1)
    b = InvocationNode("b", (b_input.use_id,), ("result",), 1)
    rejected_a = ArtifactLeafNode("bad-a", ("artifact",))
    rejected_b = ArtifactLeafNode("bad-b", ("artifact",))
    problem = OracleProblem.bind(
        "complete-blockers", uses=(root, a_input, b_input),
        root_use_ids=(root.use_id,), invocations=(a, b),
        artifact_leaves=(rejected_a, rejected_b),
        satisfaction_arcs=(_arc(root.use_id, "a"), _arc(root.use_id, "b")),
        candidate_rejections=(
            CandidateRejection(
                a_input.use_id, rejected_a.leaf_id,
                ProducerKind.ARTIFACT_LEAF, "artifact",
                ("UNITS_MISMATCH",), {"expected": "m/s", "actual": "K"}),
            CandidateRejection(
                b_input.use_id, rejected_b.leaf_id,
                ProducerKind.ARTIFACT_LEAF, "artifact",
                ("TEMPORAL_GAP",), {"gap_s": 3600}),
        ))

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.UNSATISFIABLE
    assert result.blocker_tree.complete
    nodes = _nodes(result.blocker_tree.root)
    blocked_branches = {
        value.subject_id for value in nodes
        if value.code == "INVOCATION_INPUTS_BLOCKED"
    }
    assert blocked_branches == {"a", "b"}
    rejection_codes = {
        code for value in nodes if value.code == "DIRECT_MATCH_REJECTED"
        for code in value.details["rejection_codes"]
    }
    assert rejection_codes == {"UNITS_MISMATCH", "TEMPORAL_GAP"}


def test_deployment_and_commit_are_hard_selection_constraints() -> None:
    root = RequirementUseNode("root", "result", "result")
    invocation = InvocationNode(
        "undeployable", (), ("result",), 0,
        deployable=False, zero_input_source=True)
    leaf = ArtifactLeafNode("uncommitted", ("artifact",), committed=False)
    problem = OracleProblem.bind(
        "hard-feasibility", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(invocation,), artifact_leaves=(leaf,),
        satisfaction_arcs=(
            _arc(root.use_id, invocation.invocation_id),
            _arc(root.use_id, leaf.leaf_id, port="artifact",
                 kind=ProducerKind.ARTIFACT_LEAF),
        ))

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.UNSATISFIABLE
    codes = _codes(result.blocker_tree.root)
    assert "DEPLOYMENT_INFEASIBLE" in codes
    assert "ARTIFACT_NOT_COMMITTED" in codes


def test_state_limit_returns_incomplete_incumbent_without_optimality_claim() -> None:
    root = RequirementUseNode("root", "value", "result")
    a = _source("a", 1, "result")
    b = _source("b", 2, "result")
    problem = OracleProblem.bind(
        "limited", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(a, b), satisfaction_arcs=(
            _arc(root.use_id, a.invocation_id),
            _arc(root.use_id, b.invocation_id),
        ))

    result = exhaustive_enumerate(problem, state_limit=2)

    assert result.status is OracleStatus.INCOMPLETE
    assert not result.complete
    assert result.incumbent_plan is not None
    assert result.incumbent_plan.discovery_complete is False
    assert not result.blocker_tree.complete
    assert "STATE_SPACE_LIMIT" in _codes(result.blocker_tree.root)
    with pytest.raises(RuntimeError, match="proven-optimal"):
        _ = result.optimal_plan


def test_candidate_and_bound_plan_identity_round_trip_and_tamper_rejection() -> None:
    root = RequirementUseNode("root", "value", "result")
    source = _source("source", 7, "result")
    problem = OracleProblem.bind(
        "identity", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(source,), satisfaction_arcs=(
            _arc(root.use_id, source.invocation_id),),
        snapshot_refs=(PlanSnapshotRef("contracts", "contract-schema:v1"),))
    candidate = exhaustive_enumerate(problem).optimal_plan

    assert CandidateDerivationPlan.from_dict(candidate.to_dict()) == candidate
    tampered = copy.deepcopy(candidate.to_dict())
    tampered["total_cost_units"] = 8
    with pytest.raises(ValueError, match="identity does not verify"):
        CandidateDerivationPlan.from_dict(tampered)

    execution = BoundInvocationBinding(
        invocation_id="source",
        component_id="synthetic.constant.v1",
        component_version="1.0.0",
        implementation_digest="a" * 64,
        operation_key="synthetic.constant.v1",
        runtime_parameters={"value": 7},
    )
    bound = BoundDerivationPlan.bind(
        candidate, invocation_bindings=(execution,), artifact_bindings=(),
        scientific_snapshot_refs=(PlanSnapshotRef(
            "implementation", "synthetic-implementation:v1"),))
    assert BoundDerivationPlan.from_dict(bound.to_dict()) == bound

    deployment_binding = InvocationDeploymentBinding(
        invocation_id="source",
        execution_profile_id="synthetic-profile:v1",
        deployment_class_id="private-node",
        resource_request={"cpu_cores": 1, "memory_mb": 64},
    )
    deployment = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", "private-node-snapshot:v1"),
        invocation_bindings=(deployment_binding,),
    )
    assert DeploymentPlan.from_dict(deployment.to_dict()) == deployment

    rescheduled = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=deployment.deployment_snapshot_ref,
        invocation_bindings=(dataclasses_replace(
            deployment_binding,
            resource_request={"cpu_cores": 2, "memory_mb": 128}),),
    )
    assert rescheduled.deployment_plan_id != deployment.deployment_plan_id
    assert rescheduled.bound_plan_id == deployment.bound_plan_id
    assert bound.bound_plan_id == BoundDerivationPlan.from_dict(
        bound.to_dict()).bound_plan_id


def test_all_leaf_bound_plan_has_exact_manifest_identity() -> None:
    root = RequirementUseNode("root", "value", "result")
    leaf = ArtifactLeafNode(
        "leaf", ("artifact",), cost_units=0, committed=True)
    problem = OracleProblem.bind(
        "leaf-plan", uses=(root,), root_use_ids=(root.use_id,),
        artifact_leaves=(leaf,), satisfaction_arcs=(
            _arc(root.use_id, leaf.leaf_id, port="artifact",
                 kind=ProducerKind.ARTIFACT_LEAF),))
    candidate = exhaustive_enumerate(problem).optimal_plan
    artifact = ArtifactLeafBinding(
        "leaf", "descriptor:v1", "b" * 64, "c" * 64)

    bound = BoundDerivationPlan.bind(
        candidate, invocation_bindings=(), artifact_bindings=(artifact,))

    assert bound.candidate_plan.selected_artifact_leaf_ids == ("leaf",)
    assert BoundDerivationPlan.from_dict(bound.to_dict()) == bound


def test_typed_contract_and_capability_adapters_preserve_proof_and_use_ids() -> None:
    # Reuse the capability lane's fixture to exercise the public API boundary,
    # while keeping the oracle itself explicit (no recursive discovery here).
    from tests.test_stage2_capabilities import (
        _pair_spec, _requirement, _site,
    )
    from capabilities import DeploymentCapabilitySnapshot

    spec, profile = _pair_spec()
    requirement = _requirement("scalar.left")
    accepted = CapabilityCatalog.freeze((spec,), (profile,)).bind_candidates(
        spec.spec_id, "left", requirement).accepted[0]
    use = RequirementUse("root", "result", requirement)

    use_node = RequirementUseNode.from_requirement_use(use)
    deployment = DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T00:00:00Z", (_site(profile),))
    invocation_node = InvocationNode.from_bound_invocation(
        accepted.invocation,
        deployment=deployment.check(profile),
    )
    arc = SatisfactionArc.from_compatibility(
        use, accepted.invocation, "left", accepted.compatibility)
    problem = OracleProblem.bind(
        "typed-adapters", uses=(use_node,),
        root_use_ids=(use_node.use_id,), invocations=(invocation_node,),
        satisfaction_arcs=(arc,),
        snapshot_refs=(PlanSnapshotRef(
            "selection_deployment_feasibility", deployment.snapshot_id),))

    result = exhaustive_enumerate(problem)

    assert result.status is OracleStatus.OPTIMAL
    assert use_node.use_id == use.requirement_use_id
    assert invocation_node.invocation_id == accepted.invocation.invocation_key
    payload = result.optimal_plan.compatibility_proofs[0].payload
    assert payload["proof_id"] == accepted.compatibility.proof_id
    assert payload["satisfied"] is True
    assert strict_copy(payload["proof"]) == accepted.compatibility.to_dict()
