"""Runnable compiled native-pointer chain -> artifact re-plan proof."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from artifacts import (
    ArtifactRegistry,
    ArtifactTargetCoordinator,
    ArtifactWorkflowResolver,
    RuntimeArtifactEventBridge,
)
from capabilities import (
    BinderRef,
    BindingParameterization,
    BoundInvocation,
    CapabilityCatalog,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    InputPortTemplate,
    ParameterField,
    ParameterKind,
    ParameterSchema,
)
from composition import CompilationResult, CompilationStatus, compile_bound_plan
from contracts import RequirementUse
from engine.runtime import NativeFilePointer, WorkflowController
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


@dataclass(frozen=True)
class Stage10CDemoPlan:
    native_path: Path
    pointer: NativeFilePointer
    root_use: RequirementUse
    catalog: CapabilityCatalog
    deployment_snapshot: DeploymentCapabilitySnapshot
    selected_invocations: tuple[BoundInvocation, ...]
    compilation: CompilationResult
    discovery_certificate: DiscoveryCertificate
    discovery_universe: DiscoveryUniverseContract


def build_demo_plan(native_path: str | Path) -> Stage10CDemoPlan:
    """Resolve, bind, deploy, and compile one exact no-copy pointer chain."""
    native = Path(native_path).resolve()
    content = native.read_bytes()
    pointer = NativeFilePointer.bind(
        native,
        "application/json",
        content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        metadata={"variable": "sum"},
    )
    descriptor = _descriptor("example.scalar.sum")
    exact_requirement = replace(
        _requirement("example.scalar.sum"),
        exact_descriptor_id=descriptor.descriptor_id,
    )
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
        input_ports=(InputPortTemplate("source", exact_requirement),),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema(),
        parameterizations=(BindingParameterization(
            {}, {"cost_units": 1}),),
        execution_profile_id=identity_profile.profile_id,
    )
    catalog = CapabilityCatalog.freeze(
        (source_spec, identity_spec), (source_profile, identity_profile))
    deployment_snapshot = _deployment_snapshot(catalog)
    root_use = RequirementUse(
        "stage10c-root", "result", exact_requirement)
    identity_invocation = BoundInvocation.bind(
        identity_spec, identity_spec.parameterizations[0])
    constraints = SelectionConstraints.bind(required_satisfactions=(
        SatisfactionArcSelectionRef(
            root_use.requirement_use_id,
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
    ).resolve((root_use,), constraints=constraints)
    if (outcome.status is not ResolutionStatus.READY
            or not outcome.eligible_for_binding
            or outcome.selection.plan is None):
        raise RuntimeError("Stage-10C demo did not resolve a bindable plan")
    plan = outcome.selection.plan
    selected_ids = set(plan.selected_invocation_ids)
    selected = tuple(sorted(
        (node.invocation for node in outcome.hypergraph.invocation_nodes
         if node.invocation_id in selected_ids),
        key=lambda value: value.invocation_key,
    ))
    if {value.implementation.operation_key for value in selected} != {
            "native.file_pointer.v1",
            "native.file_pointer_identity.v1"}:
        raise RuntimeError("Stage-10C demo did not select the pointer chain")
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
        root_uses=(root_use,),
    )
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None
            or compilation.authority is None):
        raise RuntimeError(compilation.record.message)
    return Stage10CDemoPlan(
        native,
        pointer,
        root_use,
        catalog,
        deployment_snapshot,
        selected,
        compilation,
        certificate,
        universe,
    )


def _bound_invocation_binding(invocation: BoundInvocation):
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


def run_demo(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    native = root / "native-sum.json"
    native.write_text("42", encoding="utf-8")
    demo = build_demo_plan(native)
    registry = ArtifactRegistry(root / "artifacts.sqlite")
    resolver = ArtifactWorkflowResolver(
        demo.catalog,
        demo.deployment_snapshot,
        registry,
        discovery_certificate=demo.discovery_certificate,
        discovery_universe=demo.discovery_universe,
    )
    coordinator = ArtifactTargetCoordinator(root / "targets.sqlite", resolver)
    before = coordinator.submit_target((demo.root_use,))
    graph = demo.compilation.graph
    authority = demo.compilation.authority
    assert graph is not None and authority is not None
    with WorkflowController(
            root / "runtime",
            artifact_commit_observer=RuntimeArtifactEventBridge(
                coordinator,
                compilation_authorities=(authority,))) as controller:
        run_id = controller.create_run(graph)
        run_state = controller.run_until_terminal(run_id)
        downstream_id = next(
            value.invocation_key for value in demo.selected_invocations
            if value.implementation.operation_key
            == "native.file_pointer_identity.v1")
        downstream_task = graph.task_by_key(dict(
            demo.compilation.record.invocation_task_keys)[downstream_id])
        downstream_row = controller.store.committed_output(
            run_id, downstream_task.task_id, "result")
        if downstream_row is None:
            raise RuntimeError("Stage-10C downstream output was not committed")
        downstream_stage1_artifact_id = downstream_row["artifact_id"]

    after = coordinator.target(before.request.target_id)
    records = registry.records()
    record = next(value for value in records
                  if value.producer_id == downstream_id)
    return {
        "schema": "stage10c-demo-result-v2",
        "run_id": run_id,
        "run_state": run_state.value,
        "before_status": before.status.value,
        "before_cost_units": before.workflow_manifest.total_cost_units,
        "after_status": after.status.value,
        "after_cost_units": after.workflow_manifest.total_cost_units,
        "artifact_record_id": record.record_id,
        "artifact_record_count": len(records),
        "native_location": record.location,
        "native_location_unchanged": record.location == str(native),
        "runtime_provenance_bound":
            registry.runtime_artifact_id(
                run_id, downstream_stage1_artifact_id) == record.artifact_id,
        "stage10_lineage_bound": len(record.inputs) == 1,
        "compilation_authority_id": authority.authority_id,
        "native_payload_copied": False,
        "transformation_selected": False,
        "cube_write": False,
    }


__all__ = ["Stage10CDemoPlan", "build_demo_plan", "run_demo"]
