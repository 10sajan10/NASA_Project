"""Stage-2 compiler and real Stage-1-kernel integration acceptance tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

from stage2.demo import build_demo_plan, run_demo

from composition import (
    ArtifactLeafNode,
    CompilationStatus,
    OracleProblem,
    RequirementUseNode,
    SatisfactionArc,
    compile_bound_plan,
    exhaustive_enumerate,
    validate_compatibility_record,
)
from capabilities import ArtifactLeaf
from contracts import RequirementUse, direct_match
from plans import (
    ArtifactLeafBinding,
    BoundDerivationPlan,
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    DeploymentPlan,
    ProducerKind,
)


def test_oracle_selects_coproducer_and_compiles_one_task_per_invocation():
    demo = build_demo_plan()
    candidate = demo.bound_plan.candidate_plan
    graph = demo.compilation.graph

    assert graph is not None
    assert candidate.total_cost_units == 3
    assert set(candidate.selected_invocation_ids) == {
        demo.pair_invocation_id, demo.add_invocation_id}
    assert candidate.selected_artifact_leaf_ids == ()
    assert demo.offered_leaf_id in {
        value.leaf_id for value in demo.oracle_problem.artifact_leaves}
    assert len(graph.tasks) == 2
    assert demo.compilation.record.bound_plan_id == demo.bound_plan.bound_plan_id
    assert demo.compilation.record.deployment_plan_id == (
        demo.deployment_plan.deployment_plan_id)

    mapping = dict(demo.compilation.record.invocation_task_keys)
    pair_task = graph.task_by_key(mapping[demo.pair_invocation_id])
    add_task = graph.task_by_key(mapping[demo.add_invocation_id])
    assert {output.output_name for output in pair_task.outputs} == {
        "left", "right"}
    assert {value.upstream_task for value in add_task.inputs} == {pair_task.key}
    assert {value.upstream_output for value in add_task.inputs} == {
        "left", "right"}
    assert demo.compilation.record.output_descriptor_bindings == tuple(sorted(
        (invocation.invocation_key, output.port_id,
         output.descriptor.descriptor_id)
        for invocation in demo.selected_invocations
        for output in invocation.outputs
    ))
    first_descriptor = demo.compilation.record.output_descriptor_bindings[0]
    changed_descriptors = (
        (first_descriptor[0], first_descriptor[1], "0" * 64),
        *demo.compilation.record.output_descriptor_bindings[1:],
    )
    assert replace(
        demo.compilation.record,
        output_descriptor_bindings=changed_descriptors,
    ).record_id != demo.compilation.record.record_id


def test_compiled_stage2_plan_executes_through_durable_stage1_kernel(tmp_path):
    result = run_demo(tmp_path)

    assert result["oracle_status"] == "OPTIMAL"
    assert result["run_state"] == "SUCCEEDED"
    assert result["task_count"] == 2
    assert result["attempt_count"] == 2
    assert result["result"] == 42


def test_root_artifact_plan_requires_trusted_commit_verification():
    demo = build_demo_plan()
    leaf_arc = next(value for value in demo.oracle_problem.satisfaction_arcs
                    if value.producer_id == demo.offered_leaf_id)
    typed_use = next(
        use for invocation in demo.selected_invocations
        for use in invocation.input_uses
        if use.requirement_use_id == leaf_arc.use_id)
    typed_leaf = ArtifactLeaf.bind(
        artifact_id="1" * 64,
        manifest_root_sha256="2" * 64,
        descriptor=next(
            output.descriptor
            for invocation in demo.selected_invocations
            for output in invocation.outputs
            if output.port_id == "left"),
    )
    root_use = RequirementUse("root", "result", typed_use.requirement)
    root = RequirementUseNode.from_requirement_use(root_use)
    proof = direct_match(typed_leaf.descriptor, typed_use.requirement)
    arc = SatisfactionArc.from_compatibility(
        root_use, typed_leaf, "artifact", proof)
    # This manual oracle node stands in for a future trusted commit-index
    # attestation. ArtifactLeaf.from_* alone intentionally cannot assert it.
    leaf = ArtifactLeafNode(
        typed_leaf.leaf_id, ("artifact",), committed=True)
    problem = OracleProblem.bind(
        "root-leaf", uses=(root,), root_use_ids=(root.use_id,),
        artifact_leaves=(leaf,), satisfaction_arcs=(arc,))
    candidate = exhaustive_enumerate(problem).optimal_plan
    bound = BoundDerivationPlan.bind(
        candidate,
        invocation_bindings=(),
        artifact_bindings=(ArtifactLeafBinding(
            leaf.leaf_id, typed_leaf.descriptor.descriptor_id,
            typed_leaf.manifest_root_sha256, "d" * 64),),
    )

    result = compile_bound_plan(
        bound, (), root_uses=(root_use,), artifact_leaves=(typed_leaf,))

    assert result.record.status is (
        CompilationStatus.ARTIFACT_COMMIT_UNVERIFIED)
    assert result.graph is None
    assert result.record.root_artifact_leaf_ids == (leaf.leaf_id,)
    assert result.record.root_bindings[0][1] == (
        "DECLARED_ARTIFACT_MANIFEST_UNVERIFIED")
    assert "trusted store" in result.record.message


def test_artifact_feeding_task_is_explicitly_unsupported_not_reexecuted():
    demo = build_demo_plan()
    original = demo.bound_plan.candidate_plan
    leaf_arc = next(
        value for value in demo.oracle_problem.satisfaction_arcs
        if value.producer_id == demo.offered_leaf_id)
    left_use_id = leaf_arc.use_id
    replacements = []
    proofs = []
    for satisfaction in original.satisfactions:
        if satisfaction.use_id == left_use_id:
            replacement = type(satisfaction)(
                use_id=left_use_id,
                kind=satisfaction.kind,
                outputs=(leaf_arc.output_ref,),
            )
            replacements.append(replacement)
            proofs.append(leaf_arc.proof)
        else:
            replacements.append(satisfaction)
            proofs.extend(
                next(value for value in original.compatibility_proofs
                     if value.proof_id == output.proof_id)
                for output in satisfaction.outputs)
    candidate = CandidateDerivationPlan.bind(
        root_use_ids=original.root_use_ids,
        root_requirements=original.root_requirements,
        oracle_problem_id=original.oracle_problem_id,
        discovery_complete=original.discovery_complete,
        selected_invocation_ids=original.selected_invocation_ids,
        selected_artifact_leaf_ids=(demo.offered_leaf_id,),
        satisfactions=replacements,
        compatibility_proofs=proofs,
        snapshot_refs=original.snapshot_refs,
        total_cost_units=original.total_cost_units,
        selection_signature=original.selection_signature,
    )
    bound = BoundDerivationPlan.bind(
        candidate,
        invocation_bindings=demo.bound_plan.invocation_bindings,
        artifact_bindings=(ArtifactLeafBinding(
            demo.offered_leaf_id,
                next(value.proof.payload["proof"]["descriptor_id"]
                     for value in (leaf_arc,)),
                demo.offered_leaf.manifest_root_sha256, "f" * 64),),
        scientific_snapshot_refs=demo.bound_plan.scientific_snapshot_refs,
    )
    deployment_plan = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=demo.deployment_plan.deployment_snapshot_ref,
        invocation_bindings=demo.deployment_plan.invocation_bindings,
    )

    result = compile_bound_plan(
        bound,
        demo.selected_invocations,
        deployment_plan=deployment_plan,
        deployment_snapshot=demo.deployment_snapshot,
        execution_profiles=demo.catalog.execution_profiles,
        root_uses=(demo.root_use,),
        artifact_leaves=(demo.offered_leaf,),
    )

    assert result.record.status is (
        CompilationStatus.BRIDGE_EXTERNAL_LEAF_UNSUPPORTED)
    assert result.graph is None
    assert result.record.output_descriptor_bindings == tuple(sorted(
        (invocation.invocation_key, output.port_id,
         output.descriptor.descriptor_id)
        for invocation in demo.selected_invocations
        for output in invocation.outputs
    ))


def test_compiler_rejects_forged_selected_proof():
    with pytest.raises(ValueError, match="unsupported proof wrapper"):
        CompatibilityProofRecord.bind({
            "satisfied": True,
            "forged": "not-a-typed-direct-match-proof",
        })


def test_graph_identity_follows_bound_plan_not_display_name():
    demo = build_demo_plan()
    first = compile_bound_plan(
        demo.bound_plan,
        demo.selected_invocations,
        deployment_plan=demo.deployment_plan,
        name="display-one",
        deployment_snapshot=demo.deployment_snapshot,
        execution_profiles=demo.catalog.execution_profiles,
        root_uses=(demo.root_use,),
    )
    second = compile_bound_plan(
        demo.bound_plan,
        demo.selected_invocations,
        deployment_plan=demo.deployment_plan,
        name="display-two",
        deployment_snapshot=demo.deployment_snapshot,
        execution_profiles=demo.catalog.execution_profiles,
        root_uses=(demo.root_use,),
    )

    assert first.graph is not None and second.graph is not None
    assert first.graph.plan_id == second.graph.plan_id


def test_compiler_rejects_unbound_deployment_and_oversized_resources():
    demo = build_demo_plan()
    with pytest.raises(ValueError, match="exact DeploymentPlan"):
        compile_bound_plan(
            demo.bound_plan, demo.selected_invocations,
            root_uses=(demo.root_use,))

    changed = tuple(
        replace(value, resource_request={
            **dict(value.resource_request), "memory_mb": 999999})
        if value.invocation_id == demo.add_invocation_id else value
        for value in demo.deployment_plan.invocation_bindings)
    oversized = DeploymentPlan.bind(
        demo.bound_plan,
        deployment_snapshot_ref=
            demo.deployment_plan.deployment_snapshot_ref,
        invocation_bindings=changed,
    )
    assert oversized.deployment_plan_id != demo.deployment_plan.deployment_plan_id
    assert demo.bound_plan.bound_plan_id == oversized.bound_plan_id
    with pytest.raises(ValueError, match="resource"):
        compile_bound_plan(
            demo.bound_plan,
            demo.selected_invocations,
            deployment_plan=oversized,
            deployment_snapshot=demo.deployment_snapshot,
            execution_profiles=demo.catalog.execution_profiles,
            root_uses=(demo.root_use,),
        )


def test_compiler_rejects_non_json_executable_output_representation():
    demo = build_demo_plan()
    output = demo.selected_invocations[0].outputs[0]
    # Simulate an untrusted/tampered typed input reaching the compiler.  The
    # representation gate must fail before finite_json is hard-coded.
    object.__setattr__(
        output.descriptor, "representation", "application/octet-stream")

    with pytest.raises(
            ValueError, match="unsupported executable output representation"):
        compile_bound_plan(
            demo.bound_plan,
            demo.selected_invocations,
            deployment_plan=demo.deployment_plan,
            deployment_snapshot=demo.deployment_snapshot,
            execution_profiles=demo.catalog.execution_profiles,
            root_uses=(demo.root_use,),
        )


def test_compiler_rejects_evidence_bound_proof_without_replay_inputs():
    demo = build_demo_plan()
    original = demo.bound_plan.candidate_plan
    old_record = original.compatibility_proofs[0]
    old_proof = validate_compatibility_record(old_record)
    evidence_proof = replace(
        old_proof, evidence_profile_id="evidence-profile-for-replay-test")
    evidence_record = CompatibilityProofRecord.from_compatibility(
        evidence_proof)

    satisfactions = tuple(
        replace(satisfaction, outputs=tuple(
            replace(output, proof_id=evidence_record.proof_id)
            if output.proof_id == old_record.proof_id else output
            for output in satisfaction.outputs
        ))
        for satisfaction in original.satisfactions
    )
    candidate = CandidateDerivationPlan.bind(
        root_use_ids=original.root_use_ids,
        root_requirements=original.root_requirements,
        oracle_problem_id=original.oracle_problem_id,
        discovery_complete=original.discovery_complete,
        selected_invocation_ids=original.selected_invocation_ids,
        selected_artifact_leaf_ids=original.selected_artifact_leaf_ids,
        satisfactions=satisfactions,
        compatibility_proofs=(evidence_record, *(
            value for value in original.compatibility_proofs
            if value.proof_id != old_record.proof_id)),
        snapshot_refs=original.snapshot_refs,
        total_cost_units=original.total_cost_units,
        selection_signature=original.selection_signature,
    )
    bound = BoundDerivationPlan.bind(
        candidate,
        invocation_bindings=demo.bound_plan.invocation_bindings,
        artifact_bindings=(),
        scientific_snapshot_refs=demo.bound_plan.scientific_snapshot_refs,
    )
    deployment_plan = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=demo.deployment_plan.deployment_snapshot_ref,
        invocation_bindings=demo.deployment_plan.invocation_bindings,
    )

    with pytest.raises(ValueError, match="evidence-bound proof replay"):
        compile_bound_plan(
            bound,
            demo.selected_invocations,
            deployment_plan=deployment_plan,
            deployment_snapshot=demo.deployment_snapshot,
            execution_profiles=demo.catalog.execution_profiles,
            root_uses=(demo.root_use,),
        )
