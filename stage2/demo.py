"""Consequence-free end-to-end Stage-2 composition demonstration.

The explicit candidate graph contains:

* one already committed left-value artifact;
* individual left and right constant producers;
* one cheaper producer that co-produces left and right; and
* one add consumer that satisfies the root request.

The exhaustive oracle must select ``pair + add``.  The selected scientific
plan then compiles to exactly two Stage-1 subprocess tasks and returns 42.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capabilities import (
    ArtifactLeaf,
    BinderRef,
    BindingCandidate,
    BindingParameterization,
    BoundInvocation,
    CapabilityCatalog,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    ExecutionProfile,
    ImplementationRef,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
)
from composition import (
    ArtifactLeafNode,
    CompilationResult,
    CompilationStatus,
    InvocationNode,
    OracleProblem,
    OracleStatus,
    RequirementUseNode,
    SatisfactionArc,
    compile_bound_plan,
    exhaustive_enumerate,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    EvidenceRequirement,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
    direct_match,
)
from engine.runtime import RunState, WorkflowController
from plans import (
    BoundInvocationBinding,
    BoundDerivationPlan,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
)


@dataclass(frozen=True)
class Stage2DemoPlan:
    catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    oracle_problem: OracleProblem
    oracle_status: OracleStatus
    candidate_plan_id: str
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation: CompilationResult
    pair_invocation_id: str
    add_invocation_id: str
    offered_leaf_id: str
    root_use: RequirementUse
    offered_leaf: ArtifactLeaf


def build_demo_plan() -> Stage2DemoPlan:
    left_descriptor = _descriptor("synthetic.scalar.left")
    right_descriptor = _descriptor("synthetic.scalar.right")
    sum_descriptor = _descriptor("synthetic.scalar.sum")
    left_requirement = _requirement("synthetic.scalar.left")
    right_requirement = _requirement("synthetic.scalar.right")
    sum_requirement = _requirement("synthetic.scalar.sum")

    pair_spec, pair_profile = _pair_spec(
        left_descriptor, right_descriptor)
    left_spec, left_profile = _constant_spec(
        "synthetic-left-constant", left_descriptor, 20)
    right_spec, right_profile = _constant_spec(
        "synthetic-right-constant", right_descriptor, 22)
    add_spec, add_profile = _add_spec(
        left_requirement, right_requirement, sum_descriptor)
    profiles = {
        value.profile_id: value for value in (
            pair_profile, left_profile, right_profile, add_profile)
    }
    catalog = CapabilityCatalog.freeze(
        (pair_spec, left_spec, right_spec, add_spec),
        profiles.values(),
    )
    deployment = _deployment_snapshot(catalog)

    pair_left = _one_binding(
        catalog, pair_spec, "left", left_requirement, deployment)
    pair_right = _one_binding(
        catalog, pair_spec, "right", right_requirement, deployment)
    if pair_left.invocation != pair_right.invocation:
        raise AssertionError("one pair parameterization produced two invocations")
    pair = pair_left.invocation
    left_constant = _one_binding(
        catalog, left_spec, "result", left_requirement, deployment)
    right_constant = _one_binding(
        catalog, right_spec, "result", right_requirement, deployment)
    add_binding = _one_binding(
        catalog, add_spec, "result", sum_requirement, deployment)
    add = add_binding.invocation

    root = RequirementUse("demo-root", "result", sum_requirement)
    root_node = RequirementUseNode.from_requirement_use(root)
    add_uses = tuple(RequirementUseNode.from_requirement_use(
        use, owner_invocation_id=add.invocation_key)
        for use in add.input_uses)
    use_by_port = {use.port_id: use for use in add.input_uses}

    leaf = ArtifactLeaf.bind(
        artifact_id="a" * 64,
        manifest_root_sha256="b" * 64,
        descriptor=left_descriptor,
    )
    leaf_proof = direct_match(leaf.descriptor, left_requirement)
    if not leaf_proof.satisfied:
        raise AssertionError("synthetic leaf must directly match left input")

    invocation_candidates = (
        InvocationNode.from_bound_invocation(
            pair,
            deployment=deployment.check(pair_profile),
        ),
        InvocationNode.from_bound_invocation(
            left_constant.invocation,
            deployment=deployment.check(left_profile),
        ),
        InvocationNode.from_bound_invocation(
            right_constant.invocation,
            deployment=deployment.check(right_profile),
        ),
        InvocationNode.from_bound_invocation(
            add,
            deployment=deployment.check(add_profile)),
    )
    arcs = (
        SatisfactionArc.from_compatibility(
            root, add, "result", add_binding.compatibility),
        SatisfactionArc.from_compatibility(
            use_by_port["left"], pair, "left", pair_left.compatibility),
        SatisfactionArc.from_compatibility(
            use_by_port["right"], pair, "right", pair_right.compatibility),
        SatisfactionArc.from_compatibility(
            use_by_port["left"], left_constant.invocation, "result",
            left_constant.compatibility),
        SatisfactionArc.from_compatibility(
            use_by_port["right"], right_constant.invocation, "result",
            right_constant.compatibility),
        SatisfactionArc.from_compatibility(
            use_by_port["left"], leaf, "artifact", leaf_proof),
    )
    problem = OracleProblem.bind(
        "stage2-pair-plus-add-demo",
        uses=(root_node, *add_uses),
        root_use_ids=(root_node.use_id,),
        invocations=invocation_candidates,
        artifact_leaves=(ArtifactLeafNode.from_artifact_leaf(
            leaf, output_port_id="artifact", cost_units=3),),
        satisfaction_arcs=arcs,
        snapshot_refs=(
            PlanSnapshotRef("capability_catalog", catalog.catalog_id),
            PlanSnapshotRef(
                "selection_deployment_feasibility", deployment.snapshot_id),
        ),
    )
    result = exhaustive_enumerate(problem)
    if result.status is not OracleStatus.OPTIMAL:
        raise RuntimeError(
            f"Stage-2 demonstration was not solved exactly: {result.status}")
    candidate = result.optimal_plan
    selected_ids = set(candidate.selected_invocation_ids)
    if selected_ids != {pair.invocation_key, add.invocation_key}:
        raise RuntimeError(
            "exact oracle did not select the globally cheapest co-produced plan")
    selected = tuple(sorted((pair, add), key=lambda value: value.invocation_key))
    binding_by_invocation = {
        pair.invocation_key: _bound_invocation_binding(pair),
        add.invocation_key: _bound_invocation_binding(add),
    }
    bound = BoundDerivationPlan.bind(
        candidate,
        invocation_bindings=tuple(
            binding_by_invocation[value] for value in sorted(selected_ids)),
        artifact_bindings=(),
        scientific_snapshot_refs=(PlanSnapshotRef(
            "capability_catalog", catalog.catalog_id),),
    )
    deployment_plan = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", deployment.snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(value) for value in selected),
    )
    compilation = compile_bound_plan(
        bound,
        selected,
        deployment_plan=deployment_plan,
        name="stage2-pair-add-demo",
        deployment_snapshot=deployment,
        execution_profiles=catalog.execution_profiles,
        root_uses=(root,),
        artifact_leaves=(),
    )
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None):
        raise RuntimeError(compilation.record.message)
    return Stage2DemoPlan(
        catalog=catalog,
        deployment_snapshot=deployment,
        oracle_problem=problem,
        oracle_status=result.status,
        candidate_plan_id=candidate.plan_id,
        selected_invocations=selected,
        bound_plan=bound,
        deployment_plan=deployment_plan,
        compilation=compilation,
        pair_invocation_id=pair.invocation_key,
        add_invocation_id=add.invocation_key,
        offered_leaf_id=leaf.leaf_id,
        root_use=root,
        offered_leaf=leaf,
    )


def run_demo(runtime_root: Path | str) -> dict[str, Any]:
    demo = build_demo_plan()
    graph = demo.compilation.graph
    assert graph is not None
    mapping = dict(demo.compilation.record.invocation_task_keys)
    add_task = graph.task_by_key(mapping[demo.add_invocation_id])
    with WorkflowController(Path(runtime_root)) as controller:
        run_id = controller.create_run(graph)
        state = controller.run_until_terminal(run_id, timeout_s=30)
        if state is not RunState.SUCCEEDED:
            raise RuntimeError(f"Stage-2 compiled run failed: {state.value}")
        result = controller.output_value(run_id, add_task.task_id, "result")
        with controller.store.connect() as con:
            attempts = int(con.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0])
    return {
        "schema": "stage2-demo-result-v1",
        "oracle_status": demo.oracle_status.value,
        "catalog_id": demo.catalog.catalog_id,
        "oracle_problem_id": demo.oracle_problem.problem_id,
        "candidate_plan_id": demo.candidate_plan_id,
        "bound_plan_id": demo.bound_plan.bound_plan_id,
        "deployment_plan_id": demo.deployment_plan.deployment_plan_id,
        "compilation_record_id": demo.compilation.record.record_id,
        "stage1_graph_id": graph.plan_id,
        "selected_invocation_ids": [
            value.invocation_key for value in demo.selected_invocations],
        "offered_but_unselected_artifact_leaf_id": demo.offered_leaf_id,
        "task_count": len(graph.tasks),
        "attempt_count": attempts,
        "result": result,
        "run_state": state.value,
    }


def _descriptor(concept: str) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept,
        schema_version="synthetic-scalar-v1",
        representation="application/json",
        units="1",
        spatial_support=BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1")),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None,
        grid=None,
        native_resolution=None,
        origin=OriginClass.SYNTHETIC,
        missingness=Missingness(MissingnessStatus.COMPLETE),
    )


def _requirement(concept: str) -> Requirement:
    return Requirement(
        concept_id=concept,
        accepted_schema_versions=("synthetic-scalar-v1",),
        representation=ValueConstraint.exact("application/json"),
        units=ValueConstraint.exact("1"),
        spatial=SpatialRequirement(BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1"))),
        temporal=TemporalRequirement(TemporalKind.TIME_INVARIANT),
        vertical=None,
        allowed_origins=(OriginClass.SYNTHETIC,),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )


def _profile(operation_key: str) -> ExecutionProfile:
    implementation = ImplementationRef.from_operation_key(operation_key)
    return ExecutionProfile.bind(
        implementation,
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1,
                min_memory_mb=64,
                max_cpu_cores=1,
                max_memory_mb=128,
            ),
        ),
    )


def _pair_spec(
    left: ArtifactDescriptor,
    right: ArtifactDescriptor,
) -> tuple[CapabilitySpec, ExecutionProfile]:
    profile = _profile("synthetic.pair.v1")
    return CapabilitySpec.bind(
        capability_id="synthetic-pair",
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.pair.bind.v1"),
        input_ports=(),
        output_ports=(DescriptorTemplate("left", left),
                      DescriptorTemplate("right", right)),
        parameter_schema=ParameterSchema((
            ParameterField("left", ParameterKind.NUMBER),
            ParameterField("right", ParameterKind.NUMBER),
        )),
        parameterizations=(BindingParameterization(
            {"left": 20, "right": 22}, {"cost_units": 2}),),
        execution_profile_id=profile.profile_id,
    ), profile


def _constant_spec(
    capability_id: str,
    output: ArtifactDescriptor,
    value: int,
) -> tuple[CapabilitySpec, ExecutionProfile]:
    profile = _profile("synthetic.constant.v1")
    return CapabilitySpec.bind(
        capability_id=capability_id,
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.constant.bind.v1"),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", output),),
        parameter_schema=ParameterSchema((
            ParameterField("value", ParameterKind.NUMBER),)),
        parameterizations=(BindingParameterization(
            {"value": value}, {"cost_units": 4}),),
        execution_profile_id=profile.profile_id,
    ), profile


def _add_spec(
    left: Requirement,
    right: Requirement,
    output: ArtifactDescriptor,
) -> tuple[CapabilitySpec, ExecutionProfile]:
    profile = _profile("synthetic.add.v1")
    return CapabilitySpec.bind(
        capability_id="synthetic-add",
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.add.bind.v1"),
        input_ports=(InputPortTemplate("left", left),
                     InputPortTemplate("right", right)),
        output_ports=(DescriptorTemplate("result", output),),
        parameter_schema=ParameterSchema(),
        parameterizations=(BindingParameterization(
            {}, {"cost_units": 1}),),
        execution_profile_id=profile.profile_id,
    ), profile


def _deployment_snapshot(
    catalog: CapabilityCatalog,
) -> DeploymentCapabilitySnapshot:
    digests = tuple(sorted({
        value.implementation.implementation_sha256
        for value in catalog.execution_profiles
    }))
    site = SiteClassCapability(
        site_class_id="private-node-synthetic-cpu",
        architecture="x86_64",
        provider_kinds=("stage1-local-subprocess",),
        implementation_digests=digests,
        environment_classes=(),
        network_classes=("none",),
        credential_classes=(),
        mount_classes=(),
        policy_classes=(),
        max_cpu_cores=1,
        max_memory_mb=128,
        max_gpus=0,
    )
    return DeploymentCapabilitySnapshot.freeze(
        "2026-08-13T00:00:00Z", (site,))


def _one_binding(
    catalog: CapabilityCatalog,
    spec: CapabilitySpec,
    port: str,
    requirement: Requirement,
    deployment: DeploymentCapabilitySnapshot,
) -> BindingCandidate:
    result = catalog.bind_candidates(
        spec.spec_id,
        port,
        requirement,
        deployment_snapshot=deployment,
    )
    if result.rejected or len(result.accepted) != 1:
        raise RuntimeError(f"unexpected binding result: {result.to_dict()}")
    return result.accepted[0]


def _bound_invocation_binding(
        invocation: BoundInvocation,
) -> BoundInvocationBinding:
    component = invocation.implementation.verify_current()
    return BoundInvocationBinding(
        invocation_id=invocation.invocation_key,
        component_id=component.component_id,
        component_version=component.version,
        implementation_digest=component.implementation_digest,
        operation_key=component.operation_key,
        runtime_parameters=dict(invocation.parameters),
    )


def _deployment_binding(
        invocation: BoundInvocation,
) -> InvocationDeploymentBinding:
    return InvocationDeploymentBinding(
        invocation_id=invocation.invocation_key,
        execution_profile_id=invocation.execution_profile_id,
        deployment_class_id="private-node-synthetic-cpu",
        resource_request={
            "cpu_cores": 1,
            "memory_mb": 64,
            "gpus": 0,
            "mpi_ranks": 0,
            "walltime_s": 30,
        },
    )
