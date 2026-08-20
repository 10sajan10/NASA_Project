"""Stage 10C: compiler-authorized runtime commits emit artifact events."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from artifacts import (
    ArtifactInput,
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    RuntimeArtifactEventBridge,
    TargetStatus,
)
from capabilities import (
    BinderRef,
    BindingParameterization,
    BoundInvocation,
    CapabilityCatalog,
    CapabilitySpec,
    DescriptorTemplate,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
)
from composition import (
    CompilationAuthority,
    CompilationStatus,
    compile_bound_plan,
)
from contracts import RequirementUse, ValueConstraint
from engine.runtime import (
    BoundExecutionGraph,
    NATIVE_FILE_POINTER_VALIDATOR_KIND,
    NativeFilePointer,
    OutputSpec,
    ResourceRequest,
    RunState,
    ScientificArtifactBinding,
    TaskTemplate,
    WorkflowController,
)
from engine.runtime.operations import operation_component
from plans import (
    BoundDerivationPlan,
    BoundInvocationBinding,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
    ProducerKind,
)
from resolution import (
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    ResolutionStatus,
    SatisfactionArcSelectionRef,
    SelectionConstraints,
    WorkflowResolver,
)
from stage3.fixtures import (
    _deployment_snapshot,
    _descriptor,
    _profile,
    _requirement,
)
from stage10c import build_demo_plan


def _compiled_native_system(
        tmp_path, *, pointer_media_type="application/json",
        descriptor_media_type="application/json",
        identity_exact_descriptor=True):
    native = tmp_path / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    if (pointer_media_type == "application/json"
            and descriptor_media_type == "application/json"
            and identity_exact_descriptor):
        demo = build_demo_plan(native)
        registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
        resolver = ArtifactWorkflowResolver(
            demo.catalog,
            demo.deployment_snapshot,
            registry,
            discovery_certificate=demo.discovery_certificate,
            discovery_universe=demo.discovery_universe,
        )
        coordinator = ArtifactTargetCoordinator(
            tmp_path / "targets.sqlite", resolver)
        return (
            native, demo.pointer, demo.root_use, registry, coordinator,
            demo.compilation, demo.selected_invocations,
        )
    payload = native.read_bytes()
    pointer = NativeFilePointer.bind(
        native, pointer_media_type,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload), metadata={"variable": "sum"})
    descriptor = replace(
        _descriptor("example.scalar.sum"),
        representation=descriptor_media_type,
    )
    exact_requirement = replace(
        _requirement("example.scalar.sum"),
        representation=ValueConstraint.exact(descriptor_media_type),
        exact_descriptor_id=descriptor.descriptor_id,
    )
    identity_requirement = (
        exact_requirement if identity_exact_descriptor else
        replace(exact_requirement, exact_descriptor_id=None))
    source_profile = _profile("native.file_pointer.v1")
    identity_profile = _profile("native.file_pointer_identity.v1")
    source_spec = CapabilitySpec.bind(
        capability_id="stage10-native-pointer-source",
        capability_version="1.0.0",
        implementation=source_profile.implementation,
        binder=BinderRef.from_key("native.file_pointer.bind.v1"),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("pointer", ParameterKind.JSON),)),
        parameterizations=(BindingParameterization(
            {"pointer": pointer.to_dict()}, {"cost_units": 1}),),
        execution_profile_id=source_profile.profile_id,
    )
    identity_spec = CapabilitySpec.bind(
        capability_id="stage10-native-pointer-identity",
        capability_version="1.0.0",
        implementation=identity_profile.implementation,
        binder=BinderRef.from_key("native.file_pointer_identity.bind.v1"),
        input_ports=(InputPortTemplate("source", identity_requirement),),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema(),
        parameterizations=(BindingParameterization(
            {}, {"cost_units": 1}),),
        execution_profile_id=identity_profile.profile_id,
    )
    catalog = CapabilityCatalog.freeze(
        (source_spec, identity_spec), (source_profile, identity_profile))
    deployment_snapshot = _deployment_snapshot(catalog)
    root = RequirementUse(
        "stage10c-root", "result", exact_requirement)
    identity_invocation = BoundInvocation.bind(
        identity_spec, identity_spec.parameterizations[0])
    constraints = SelectionConstraints.bind(required_satisfactions=(
        SatisfactionArcSelectionRef(
            root.requirement_use_id,
            ProducerKind.INVOCATION,
            identity_invocation.invocation_key,
            "result",
        ),
    ))
    certificate = DiscoveryCertificate.for_base_catalog(catalog)
    universe = DiscoveryUniverseContract.declare(catalog.catalog_id)
    outcome = WorkflowResolver(
        catalog,
        deployment_snapshot,
        discovery_certificate=certificate,
        discovery_universe=universe,
    ).resolve((root,), constraints=constraints)
    assert outcome.status is ResolutionStatus.READY
    assert outcome.eligible_for_binding
    assert outcome.selection.plan is not None
    plan = outcome.selection.plan
    selected_ids = set(plan.selected_invocation_ids)
    selected = tuple(sorted(
        (node.invocation for node in outcome.hypergraph.invocation_nodes
         if node.invocation_id in selected_ids),
        key=lambda value: value.invocation_key,
    ))
    assert {value.implementation.operation_key for value in selected} == {
        "native.file_pointer.v1", "native.file_pointer_identity.v1"}
    bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=(),
        scientific_snapshot_refs=(
            PlanSnapshotRef("capability_catalog", catalog.catalog_id),
            PlanSnapshotRef("feasible_hypergraph", outcome.hypergraph.graph_id),
        ),
    )
    sites = dict(outcome.selection.deployment_choices)
    profiles = {value.profile_id: value
                for value in catalog.execution_profiles}
    deployment = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", deployment_snapshot.snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(
                value, sites[value.invocation_key],
                profiles[value.execution_profile_id])
            for value in selected),
    )
    compilation = compile_bound_plan(
        bound,
        selected,
        deployment_plan=deployment,
        deployment_snapshot=deployment_snapshot,
        execution_profiles=catalog.execution_profiles,
        root_uses=(root,),
    )
    assert compilation.record.status is CompilationStatus.COMPILED
    assert compilation.graph is not None
    assert compilation.authority is not None

    registry = ArtifactRegistry(tmp_path / "artifacts.sqlite")
    resolver = ArtifactWorkflowResolver(
        catalog,
        deployment_snapshot,
        registry,
        discovery_certificate=certificate,
        discovery_universe=universe,
    )
    coordinator = ArtifactTargetCoordinator(
        tmp_path / "targets.sqlite", resolver)
    return (
        native, pointer, root, registry, coordinator, compilation,
        selected,
    )


def _bound_invocation_binding(invocation):
    component = invocation.implementation.verify_current()
    return BoundInvocationBinding(
        invocation_id=invocation.invocation_key,
        component_id=component.component_id,
        component_version=component.version,
        implementation_digest=component.implementation_digest,
        operation_key=component.operation_key,
        runtime_parameters=dict(invocation.parameters),
    )


def _deployment_binding(invocation, site_class_id, profile):
    envelope = profile.placement.resources
    return InvocationDeploymentBinding(
        invocation_id=invocation.invocation_key,
        execution_profile_id=invocation.execution_profile_id,
        deployment_class_id=site_class_id,
        resource_request={
            "cpu_cores": envelope.min_cpu_cores,
            "memory_mb": envelope.min_memory_mb,
            "gpus": envelope.min_gpus,
            "mpi_ranks": 0,
            "walltime_s": 30,
        },
    )


def _manual_native_graph(native):
    payload = native.read_bytes()
    pointer = NativeFilePointer.bind(
        native, "application/json",
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload), metadata={"variable": "sum"})
    descriptor = _descriptor("example.scalar.sum")
    binding = ScientificArtifactBinding(
        bound_plan_id="a" * 64,
        invocation_id="b" * 64,
        capability_id="caller.manual.native-pointer",
        capability_version="1.0.0",
        evidence_profile_id="evidence:unknown",
        output_port="result",
        descriptor_id=descriptor.descriptor_id,
        descriptor=descriptor.to_dict(),
    )
    return BoundExecutionGraph.bind(
        "caller-labelled-native-output",
        (TaskTemplate(
            key="native-sum-output",
            component=operation_component("native.file_pointer.v1"),
            parameters={"pointer": pointer.to_dict()},
            outputs=(OutputSpec(
                name="result", media_type="application/json",
                validation={"kind": NATIVE_FILE_POINTER_VALIDATOR_KIND},
                scientific_binding=binding),),
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=64, walltime_s=20),
        ),),
    )


def test_compiled_native_chain_registers_stage10_lineage_namespace(tmp_path):
    (native, pointer, root, registry, coordinator, compilation,
     selected) = _compiled_native_system(tmp_path)
    target = coordinator.submit_target((root,))
    assert target.status is TargetStatus.PLANNED_WORKFLOW
    graph = compilation.graph
    authority = compilation.authority
    assert graph is not None and authority is not None
    bridge = RuntimeArtifactEventBridge(
        coordinator, compilation_authorities=(authority,))
    mapping = dict(compilation.record.invocation_task_keys)
    invocation_by_operation = {
        value.implementation.operation_key: value.invocation_key
        for value in selected}
    selected_by_operation = {
        value.implementation.operation_key: value for value in selected}
    source_task = graph.task_by_key(mapping[
        invocation_by_operation["native.file_pointer.v1"]])
    downstream_task = graph.task_by_key(mapping[
        invocation_by_operation["native.file_pointer_identity.v1"]])

    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=bridge) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert NativeFilePointer.from_dict(controller.output_value(
            run_id, downstream_task.task_id)) == pointer
        source_row = controller.store.committed_output(
            run_id, source_task.task_id, "result")
        assert source_row is not None
        stage1_source_id = source_row["artifact_id"]
        assert controller.store.task_input_artifact_ids(
            run_id, downstream_task.task_id) == (
                ("source", stage1_source_id),)

    records = registry.records()
    assert len(records) == 2
    by_producer = {value.producer_id: value for value in records}
    source_record = by_producer[
        invocation_by_operation["native.file_pointer.v1"]]
    downstream_record = by_producer[
        invocation_by_operation["native.file_pointer_identity.v1"]]
    downstream_invocation = selected_by_operation[
        "native.file_pointer_identity.v1"]
    assert downstream_record.producer_version == (
        downstream_invocation.capability_version)
    assert downstream_record.evidence_profile_id == (
        downstream_invocation.evidence_profile_id)
    assert downstream_record.metadata["scientific_provenance"] == {
        "schema": "stage10d-scientific-provenance-v1",
        "bound_plan_id": compilation.record.bound_plan_id,
        "invocation_id": downstream_invocation.invocation_key,
        "capability_id": downstream_invocation.capability_id,
        "capability_version": downstream_invocation.capability_version,
        "evidence_profile_id": downstream_invocation.evidence_profile_id,
    }
    assert downstream_record.inputs == (
        ArtifactInput("source", source_record.artifact_id),)
    assert downstream_record.inputs[0].artifact_id != stage1_source_id
    assert registry.runtime_artifact_id(
        run_id, stage1_source_id) == source_record.artifact_id
    with pytest.raises(ValueError, match="conflicting or ambiguous"):
        registry.bind_runtime_outputs(
            run_id,
            source_task.task_id,
            (("result", stage1_source_id, downstream_record),),
        )
    with pytest.raises(KeyError, match="unknown Stage-1 artifact"):
        registry.runtime_artifact_id(run_id, "f" * 64)
    assert coordinator.target(target.request.target_id).status is \
        TargetStatus.SATISFIED_BY_ARTIFACT
    assert native.read_text(encoding="utf-8") == "42"


def test_restart_replays_terminal_commit_with_exact_caller_authority(tmp_path):
    (_native, _pointer, _root, registry, coordinator,
     compilation, _selected) = _compiled_native_system(tmp_path)
    graph = compilation.graph
    authority = compilation.authority
    assert graph is not None and authority is not None
    runtime = tmp_path / "runtime"

    with WorkflowController(runtime) as first:
        run_id = first.create_run(graph)
        assert first.run_until_terminal(run_id) is RunState.SUCCEEDED
    assert registry.records() == ()

    # A new bridge/controller gets the exact compiler-issued receipt from its
    # caller.  Omitting that registry is tested as a refusal below.
    recovered_bridge = RuntimeArtifactEventBridge(
        coordinator, compilation_authorities=(authority,))
    with WorkflowController(
            runtime, artifact_commit_observer=recovered_bridge) as recovered:
        assert recovered.tick(run_id) is RunState.SUCCEEDED
        assert recovered.tick(run_id) is RunState.SUCCEEDED
    assert len(registry.records()) == 2


def test_distinct_runs_replay_same_records_with_run_scoped_mappings(tmp_path):
    (_native, _pointer, _root, registry, coordinator,
     compilation, _selected) = _compiled_native_system(tmp_path)
    graph = compilation.graph
    authority = compilation.authority
    assert graph is not None and authority is not None
    bridge = RuntimeArtifactEventBridge(
        coordinator, compilation_authorities=(authority,))
    run_ids = []
    stage1_ids = []
    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=bridge) as controller:
        for _ in range(2):
            run_id = controller.create_run(graph)
            assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
            run_ids.append(run_id)
            first = graph.tasks[0]
            row = controller.store.committed_output(
                run_id, first.task_id, "result")
            assert row is not None
            stage1_ids.append(row["artifact_id"])
    assert run_ids[0] != run_ids[1]
    assert len(registry.records()) == 2
    assert registry.runtime_artifact_id(
        run_ids[0], stage1_ids[0]) == registry.runtime_artifact_id(
            run_ids[1], stage1_ids[1])


def test_manual_scientific_labels_are_not_publication_authority(tmp_path):
    (_native, _pointer, _root, registry, coordinator,
     _compilation, _selected) = _compiled_native_system(tmp_path)
    manual_file = tmp_path / "manual.json"
    manual_file.write_text("42", encoding="utf-8")
    graph = _manual_native_graph(manual_file)
    with WorkflowController(
            tmp_path / "manual-runtime",
            artifact_commit_observer=RuntimeArtifactEventBridge(
                coordinator)) as controller:
        run_id = controller.create_run(graph)
        with pytest.raises(PermissionError, match="compiler-issued authority"):
            controller.run_until_terminal(run_id)
    assert registry.records() == ()


def test_compilation_authority_refuses_tampered_record_and_public_mint(tmp_path):
    (_native, _pointer, _root, _registry, _coordinator,
     compilation, _selected) = _compiled_native_system(tmp_path)
    authority = compilation.authority
    graph = compilation.graph
    assert authority is not None and graph is not None
    changed = replace(compilation.record, message="caller-relabelled")
    with pytest.raises(ValueError, match="another record"):
        authority.verify(changed, graph)
    with pytest.raises(PermissionError, match="compile_bound_plan"):
        CompilationAuthority(
            object(), authority_id=authority.authority_id,
            record=compilation.record)


def test_compiler_refuses_pointer_media_type_descriptor_mismatch(tmp_path):
    with pytest.raises(ValueError, match="media type disagrees"):
        _compiled_native_system(
            tmp_path,
            pointer_media_type="application/octet-stream",
            descriptor_media_type="application/json",
        )


def test_pointer_identity_capability_cannot_relabel_science(tmp_path):
    with pytest.raises(ValueError, match="exact output descriptor"):
        _compiled_native_system(
            tmp_path, identity_exact_descriptor=False)


def test_worker_refuses_native_file_changed_before_execution(tmp_path):
    (native, _pointer, _root, registry, coordinator,
     compilation, _selected) = _compiled_native_system(tmp_path)
    graph = compilation.graph
    authority = compilation.authority
    assert graph is not None and authority is not None
    native.write_text("tampered", encoding="utf-8")
    with WorkflowController(
            tmp_path / "runtime",
            artifact_commit_observer=RuntimeArtifactEventBridge(
                coordinator,
                compilation_authorities=(authority,))) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.FAILED
    assert registry.records() == ()
