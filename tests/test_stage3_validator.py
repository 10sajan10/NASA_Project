"""Backend-independent Stage-3 selected-plan validation tests."""
from __future__ import annotations

from dataclasses import replace

from capabilities import ArtifactLeaf, CapabilityCatalog
from composition import (
    ArtifactLeafNode,
    OracleProblem,
    RequirementUseNode,
    SatisfactionArc,
    exhaustive_enumerate,
)
from contracts import (
    CheckStatus,
    CompatibilityCheck,
    CompatibilityProof,
    MatchCode,
    RequirementUse,
    direct_match,
)
from plans import (
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    PlanSnapshotRef,
    SatisfactionBinding,
    SatisfactionKind,
)
from resolution import build_feasible_hypergraph, project_oracle_problem
from resolution.validator import (
    ArtifactCommitAttestation,
    SelectionConstraints,
    ValidationCode,
    validate_selected_plan,
)
from resolution.milp import (
    SatisfactionArcSelectionRef,
    SelectionConstraints as MilpSelectionConstraints,
)
from stage2.demo import build_demo_plan
from stage2.demo import (
    _add_spec,
    _constant_spec,
    _deployment_snapshot,
    _descriptor,
    _requirement,
)


def _demo_validation_arguments(demo):
    selected_profiles = {
        value.execution_profile_id for value in demo.selected_invocations}
    proofs = tuple(
        demo.deployment_snapshot.check(profile)
        for profile in demo.catalog.execution_profiles
        if profile.profile_id in selected_profiles
    )
    return {
        "requirement_uses": (demo.root_use,),
        "invocations": demo.selected_invocations,
        "execution_profiles": demo.catalog.execution_profiles,
        "deployment_snapshot": demo.deployment_snapshot,
        "deployment_proofs": proofs,
        "deployment_choices": tuple(
            (value.invocation_id, value.deployment_class_id)
            for value in demo.deployment_plan.invocation_bindings),
    }


def test_valid_demo_plan_replays_independently_and_is_deterministic():
    demo = build_demo_plan()
    arguments = _demo_validation_arguments(demo)

    first = validate_selected_plan(
        demo.bound_plan.candidate_plan, demo.oracle_problem, **arguments)
    second = validate_selected_plan(
        demo.bound_plan.candidate_plan, demo.oracle_problem, **arguments)

    assert first.valid
    assert first.blockers == ()
    assert first.recomputed_cost_units == 3
    assert first.to_dict() == second.to_dict()
    assert first.report_id == second.report_id


def test_validator_accumulates_budget_policy_and_completeness_blockers():
    demo = build_demo_plan()
    arguments = _demo_validation_arguments(demo)
    selected = set(demo.bound_plan.candidate_plan.selected_invocation_ids)
    unselected = next(
        value.invocation_id for value in demo.oracle_problem.invocations
        if value.invocation_id not in selected)
    constraints = SelectionConstraints(
        max_cost_units=2,
        include_invocation_ids=(unselected,),
        exclude_invocation_ids=(demo.add_invocation_id,),
        require_complete_universe=True,
    )

    report = validate_selected_plan(
        demo.bound_plan.candidate_plan,
        demo.oracle_problem,
        constraints=constraints,
        candidate_universe_complete=False,
        **arguments,
    )

    codes = {value.code for value in report.blockers}
    assert not report.valid
    assert ValidationCode.BUDGET_EXCEEDED in codes
    assert ValidationCode.INCLUDE_CONSTRAINT_UNSATISFIED in codes
    assert ValidationCode.EXCLUDED_PRODUCER_SELECTED in codes
    assert ValidationCode.DISCOVERY_COMPLETENESS_MISMATCH in codes
    assert ValidationCode.CANDIDATE_UNIVERSE_INCOMPLETE in codes
    assert report.blocker_tree is not None
    assert report.blocker_tree.complete
    assert report.blocker_tree.logic == "ALL"


def test_validator_independently_replays_exact_satisfaction_constraints():
    demo = build_demo_plan()
    selected = {
        (binding.use_id, output.producer_kind, output.producer_id,
         output.output_port_id)
        for binding in demo.bound_plan.candidate_plan.satisfactions
        if binding.kind is SatisfactionKind.PRODUCERS
        for output in binding.outputs
    }
    unselected_arc = next(
        arc for arc in demo.oracle_problem.satisfaction_arcs
        if (arc.use_id, arc.producer_kind, arc.producer_id,
            arc.output_port_id) not in selected)
    constraint = SatisfactionArcSelectionRef.from_arc(unselected_arc)

    report = validate_selected_plan(
        demo.bound_plan.candidate_plan,
        demo.oracle_problem,
        constraints=MilpSelectionConstraints.bind(
            required_satisfactions=(constraint,)),
        **_demo_validation_arguments(demo),
    )

    assert not report.valid
    assert ValidationCode.REQUIRED_SATISFACTION_UNSATISFIED in {
        value.code for value in report.blockers}


def test_validator_rejects_an_unbound_deployment_choice():
    demo = build_demo_plan()
    arguments = _demo_validation_arguments(demo)
    arguments["deployment_choices"] = tuple(
        (invocation_id, "site-not-in-frozen-snapshot")
        for invocation_id, _site_id in arguments["deployment_choices"])

    report = validate_selected_plan(
        demo.bound_plan.candidate_plan,
        demo.oracle_problem,
        **arguments,
    )

    assert not report.valid
    assert ValidationCode.DEPLOYMENT_CHOICE_UNKNOWN in {
        value.code for value in report.blockers}


def test_well_formed_but_fabricated_typed_proof_fails_replay():
    demo = build_demo_plan()
    original = demo.bound_plan.candidate_plan
    root_binding = next(
        value for value in original.satisfactions
        if value.use_id == demo.root_use.requirement_use_id)
    output = root_binding.outputs[0]
    producer = next(
        value for value in demo.selected_invocations
        if value.invocation_key == output.producer_id)
    descriptor = producer.output(output.output_port_id).descriptor
    fabricated = CompatibilityProof(
        requirement_id=demo.root_use.requirement.requirement_id,
        descriptor_id=descriptor.descriptor_id,
        evidence_profile_id=None,
        evidence_snapshot_id=None,
        evidence_subject_id=None,
        checks=(CompatibilityCheck(
            "fabricated", CheckStatus.PASS, MatchCode.MATCH),),
    )
    fabricated_record = CompatibilityProofRecord.from_compatibility(fabricated)
    changed_binding = replace(
        root_binding,
        outputs=(replace(
            output, proof_id=fabricated_record.proof_id),),
    )
    changed_satisfactions = tuple(
        changed_binding if value.use_id == root_binding.use_id else value
        for value in original.satisfactions)
    changed_proofs = tuple(
        [fabricated_record]
        + [value for value in original.compatibility_proofs
           if value.proof_id != output.proof_id])
    forged_plan = CandidateDerivationPlan.bind(
        root_use_ids=original.root_use_ids,
        root_requirements=original.root_requirements,
        oracle_problem_id=original.oracle_problem_id,
        discovery_complete=original.discovery_complete,
        selected_invocation_ids=original.selected_invocation_ids,
        selected_artifact_leaf_ids=original.selected_artifact_leaf_ids,
        satisfactions=changed_satisfactions,
        compatibility_proofs=changed_proofs,
        snapshot_refs=original.snapshot_refs,
        total_cost_units=original.total_cost_units,
        selection_signature=original.selection_signature,
    )

    report = validate_selected_plan(
        forged_plan,
        demo.oracle_problem,
        **_demo_validation_arguments(demo),
    )

    codes = {value.code for value in report.blockers}
    assert not report.valid
    assert ValidationCode.PROOF_REPLAY_MISMATCH in codes
    assert ValidationCode.EDGE_NOT_IN_CANDIDATE_UNIVERSE in codes


def test_artifact_declaration_requires_separate_commit_attestation():
    demo = build_demo_plan()
    leaf: ArtifactLeaf = demo.offered_leaf
    add = next(
        value for value in demo.selected_invocations if value.input_uses)
    left_use = next(value for value in add.input_uses
                    if value.port_id == "left")
    root_use = RequirementUse(
        "stage3-artifact-root", "artifact", left_use.requirement)
    proof = direct_match(leaf.descriptor, root_use.requirement)
    arc = SatisfactionArc.from_compatibility(
        root_use, leaf, "artifact", proof)
    problem = OracleProblem.bind(
        "stage3-artifact-attestation",
        uses=(RequirementUseNode.from_requirement_use(root_use),),
        root_use_ids=(root_use.requirement_use_id,),
        artifact_leaves=(ArtifactLeafNode(
            leaf.leaf_id, ("artifact",), committed=True),),
        satisfaction_arcs=(arc,),
        snapshot_refs=(PlanSnapshotRef("artifact_availability", "c" * 64),),
    )
    plan = exhaustive_enumerate(problem).optimal_plan

    missing = validate_selected_plan(
        plan,
        problem,
        requirement_uses=(root_use,),
        invocations=(),
        artifact_leaves=(leaf,),
    )
    assert not missing.valid
    assert ValidationCode.ARTIFACT_ATTESTATION_MISSING in {
        value.code for value in missing.blockers}

    attestation = ArtifactCommitAttestation.bind(
        "c" * 64, leaf, committed=True)
    verified = validate_selected_plan(
        plan,
        problem,
        requirement_uses=(root_use,),
        invocations=(),
        artifact_leaves=(leaf,),
        artifact_attestations=(attestation,),
    )
    assert verified.valid

    wrong_snapshot = ArtifactCommitAttestation.bind(
        "f" * 64, leaf, committed=True)
    rejected = validate_selected_plan(
        plan,
        problem,
        requirement_uses=(root_use,),
        invocations=(),
        artifact_leaves=(leaf,),
        artifact_attestations=(wrong_snapshot,),
    )
    assert not rejected.valid
    assert ValidationCode.ARTIFACT_AVAILABILITY_SNAPSHOT_UNBOUND in {
        value.code for value in rejected.blockers}


def test_validator_independently_rejects_selected_cycle_and_ungrounded_plan():
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
    root = RequirementUse(
        "validator-cycle-root", "result", recursive_requirement)
    graph = build_feasible_hypergraph(catalog, deployment, (root,))
    problem = project_oracle_problem(graph)
    invocations = tuple(value.invocation for value in graph.invocation_nodes)
    recursive = next(value for value in invocations if value.input_uses)
    base = next(value for value in invocations if not value.input_uses)
    recursive_input = next(
        value for value in recursive.input_uses
        if value.requirement.requirement_id
        == recursive_requirement.requirement_id)
    base_input = next(
        value for value in recursive.input_uses
        if value.requirement.requirement_id == base_requirement.requirement_id)

    def arc_for(use_id, producer_id):
        return next(value for value in problem.satisfaction_arcs
                    if value.use_id == use_id
                    and value.producer_id == producer_id)

    chosen_arcs = (
        arc_for(root.requirement_use_id, recursive.invocation_key),
        arc_for(recursive_input.requirement_use_id,
                recursive.invocation_key),
        arc_for(base_input.requirement_use_id, base.invocation_key),
    )
    satisfactions = tuple(SatisfactionBinding(
        value.use_id,
        SatisfactionKind.PRODUCERS,
        (value.output_ref,),
    ) for value in chosen_arcs)
    cyclic_plan = CandidateDerivationPlan.bind(
        root_use_ids=problem.root_use_ids,
        root_requirements=((root.requirement_use_id,
                            recursive_requirement.requirement_id),),
        oracle_problem_id=problem.problem_id,
        discovery_complete=True,
        selected_invocation_ids=(recursive.invocation_key,
                                 base.invocation_key),
        selected_artifact_leaf_ids=(),
        satisfactions=satisfactions,
        compatibility_proofs=tuple(
            {value.proof.proof_id: value.proof for value in chosen_arcs}[key]
            for key in sorted({value.proof.proof_id
                               for value in chosen_arcs})),
        snapshot_refs=problem.snapshot_refs,
        total_cost_units=sum(value.cost_units
                             for value in graph.invocation_nodes),
        # The validator recomputes this independently; a deliberately empty
        # signature also proves cycle/grounding checks are not gated on it.
        selection_signature=(),
    )
    report = validate_selected_plan(
        cyclic_plan,
        problem,
        requirement_uses=(root,),
        invocations=invocations,
        execution_profiles=catalog.execution_profiles,
        deployment_snapshot=deployment,
        deployment_proofs=graph.deployment_proofs,
    )
    assert ValidationCode.CYCLE_DETECTED in {
        value.code for value in report.blockers}
    assert ValidationCode.UNGROUNDED_DERIVATION in {
        value.code for value in report.blockers}
