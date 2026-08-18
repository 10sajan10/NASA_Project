"""Runnable Stage-3 discovery -> selection -> validation demonstration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capabilities import BoundInvocation
from composition import CompilationResult, CompilationStatus, compile_bound_plan
from composition.oracle import OracleStatus, exhaustive_enumerate
from engine.runtime import RunState, WorkflowController
from plans import (
    ArtifactLeafBinding,
    BoundDerivationPlan,
    BoundInvocationBinding,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
)
from resolution import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitRecord,
    ArtifactCommitStatus,
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    ResolutionOutcome,
    ResolutionStatus,
    WorkflowResolver,
)

from .fixtures import CompositionFixture, make_composition_fixture


@dataclass(frozen=True)
class Stage3DemoPlan:
    fixture: CompositionFixture
    availability_snapshot: ArtifactAvailabilitySnapshot
    resolution: ResolutionOutcome
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation: CompilationResult


def build_demo_plan() -> Stage3DemoPlan:
    fixture = make_composition_fixture()
    availability = ArtifactAvailabilitySnapshot.freeze(
        ArtifactCommitRecord(value.leaf_id, ArtifactCommitStatus.COMMITTED)
        for value in fixture.offered_artifact_leaves)
    resolution = WorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
        artifact_leaves=fixture.offered_artifact_leaves,
        availability_snapshot=availability,
    ).resolve(fixture.root_uses)
    if (resolution.status is not ResolutionStatus.READY
            or not resolution.eligible_for_binding
            or resolution.selection.plan is None
            or resolution.validation is None
            or not resolution.validation.valid):
        raise RuntimeError("Stage-3 resolver did not produce a validated optimum")

    # This exhaustive check is deliberately independent from the MILP.  It is
    # retained in the tiny demonstration as a correctness oracle, not used by
    # production selection.
    oracle = exhaustive_enumerate(resolution.selector_problem)
    if (oracle.status is not OracleStatus.OPTIMAL
            or oracle.optimal_plan.plan_id
            != resolution.selection.plan.plan_id):
        raise RuntimeError("MILP selection disagrees with exhaustive oracle")

    plan = resolution.selection.plan
    selected_ids = set(plan.selected_invocation_ids)
    selected = tuple(sorted(
        (value.invocation for value in resolution.hypergraph.invocation_nodes
         if value.invocation_id in selected_ids),
        key=lambda value: value.invocation_key,
    ))
    leaf_by_id = {
        value.leaf_id: value for value in fixture.offered_artifact_leaves
    }
    bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=tuple(ArtifactLeafBinding(
            leaf_id=value,
            descriptor_id=leaf_by_id[value].descriptor.descriptor_id,
            manifest_root=leaf_by_id[value].manifest_root_sha256,
            content_digest=leaf_by_id[value].artifact_id,
        ) for value in plan.selected_artifact_leaf_ids),
        scientific_snapshot_refs=(
            PlanSnapshotRef(
                "capability_catalog", fixture.catalog.catalog_id),
            PlanSnapshotRef(
                "feasible_hypergraph", resolution.hypergraph.graph_id),
        ),
    )
    site_by_invocation = dict(resolution.selection.deployment_choices)
    profile_by_id = {
        value.profile_id: value for value in fixture.catalog.execution_profiles
    }
    deployment = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", fixture.deployment_snapshot.snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(
                value,
                site_by_invocation[value.invocation_key],
                profile_by_id[value.execution_profile_id],
            ) for value in selected),
    )
    compilation = compile_bound_plan(
        bound,
        selected,
        deployment_plan=deployment,
        name="stage3-domain-neutral-composition-demo",
        deployment_snapshot=fixture.deployment_snapshot,
        execution_profiles=fixture.catalog.execution_profiles,
        root_uses=fixture.root_uses,
        artifact_leaves=tuple(
            leaf_by_id[value] for value in plan.selected_artifact_leaf_ids),
    )
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None):
        raise RuntimeError(compilation.record.message)
    return Stage3DemoPlan(
        fixture=fixture,
        availability_snapshot=availability,
        resolution=resolution,
        selected_invocations=selected,
        bound_plan=bound,
        deployment_plan=deployment,
        compilation=compilation,
    )


def run_demo(runtime_root: Path | str) -> dict[str, Any]:
    demo = build_demo_plan()
    graph = demo.compilation.graph
    assert graph is not None
    root_binding = demo.compilation.record.root_bindings[0]
    if root_binding[1] != "TASK_OUTPUT":
        raise RuntimeError("demo root did not compile to an executable output")
    task = graph.task_by_key(root_binding[2])
    with WorkflowController(Path(runtime_root)) as controller:
        run_id = controller.create_run(graph)
        run_state = controller.run_until_terminal(run_id, timeout_s=30)
        if run_state is not RunState.SUCCEEDED:
            raise RuntimeError(f"Stage-3 compiled run failed: {run_state.value}")
        value = controller.output_value(
            run_id, task.task_id, root_binding[3])
        with controller.store.connect() as connection:
            attempts = int(connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0])

    selected_capabilities = sorted(
        value.capability_id for value in demo.selected_invocations)
    return {
        "schema": "stage3-demo-result-v1",
        "resolution_status": demo.resolution.status.value,
        "discovery_complete": demo.resolution.hypergraph.discovery_complete,
        "solver_status": demo.resolution.selection.status.value,
        "globally_optimal": (
            demo.resolution.selection.globally_optimal_over_discovery_space),
        "validation_passed": demo.resolution.validation.valid,
        "eligible_for_binding": demo.resolution.eligible_for_binding,
        "resolution_id": demo.resolution.resolution_id,
        "hypergraph_id": demo.resolution.hypergraph.graph_id,
        "oracle_problem_id": demo.resolution.selector_problem.problem_id,
        "candidate_plan_id": demo.resolution.selection.plan.plan_id,
        "bound_plan_id": demo.bound_plan.bound_plan_id,
        "deployment_plan_id": demo.deployment_plan.deployment_plan_id,
        "stage1_graph_id": graph.plan_id,
        "selected_capabilities": selected_capabilities,
        "selected_cost_units": demo.resolution.selection.objective_cost_units,
        "requirements_discovered": len(
            demo.resolution.hypergraph.requirement_nodes),
        "uses_preserved": len(demo.resolution.hypergraph.use_nodes),
        "invocations_discovered": len(
            demo.resolution.hypergraph.invocation_nodes),
        "satisfaction_arcs": len(
            demo.resolution.hypergraph.satisfaction_arcs),
        "task_count": len(graph.tasks),
        "attempt_count": attempts,
        "result": value,
        "run_state": run_state.value,
        "planning_metrics_ns": demo.resolution.metrics.to_dict(),
    }


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


__all__ = ["Stage3DemoPlan", "build_demo_plan", "run_demo"]
