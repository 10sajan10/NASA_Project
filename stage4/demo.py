"""Runnable Stage-4 transformation-path demonstration.

The chain is: finite transformation closure -> Stage-3 recursive discovery ->
global selection -> independent validation -> Stage-1 compilation -> durable
execution -> validated commit.  The transformation is an ordinary costed node
throughout; no adapter converts anything privately.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from capabilities import BoundInvocation
from composition import CompilationResult, CompilationStatus, compile_bound_plan
from composition.oracle import OracleStatus, exhaustive_enumerate
from engine.runtime import RunState, WorkflowController
from plans import (
    BoundDerivationPlan,
    BoundInvocationBinding,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
)
from resolution import (
    DiscoveryLayerScope,
    DiscoveryUniverseContract,
    ResolutionOutcome,
    ResolutionStatus,
    WorkflowResolver,
)
from transformations import (
    TransformationCatalog,
    TransformationExpansion,
    TransformationDiscoveryReplay,
    TransformationSearchLimits,
    expand_transform_catalog,
)

from .fixtures import TransformationFixture, make_unit_bridge_fixture


@dataclass(frozen=True)
class Stage4DemoPlan:
    fixture: TransformationFixture
    expansion: TransformationExpansion
    resolution: ResolutionOutcome
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation: CompilationResult


def build_demo_plan(
    *,
    limits: TransformationSearchLimits = TransformationSearchLimits(),
) -> Stage4DemoPlan:
    fixture = make_unit_bridge_fixture()
    transformation_catalog = TransformationCatalog.freeze(
        fixture.transformations)
    discovery_universe = DiscoveryUniverseContract.declare(
        fixture.base_catalog.catalog_id,
        (DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=(transformation_catalog.catalog_id,),
            limits=limits.to_dict()),),
    )
    expansion = expand_transform_catalog(
        fixture.base_catalog, transformation_catalog, limits=limits)

    # The certificate binds what closure produced; the separately authored
    # universe binds what closure was required to cover.
    resolution = WorkflowResolver(
        expansion.augmented_catalog,
        fixture.deployment_snapshot,
        discovery_certificate=expansion.discovery_certificate(),
        discovery_universe=discovery_universe,
        discovery_replays=(TransformationDiscoveryReplay.bind(
            fixture.base_catalog, transformation_catalog, limits=limits),),
    ).resolve(fixture.root_uses)
    if (resolution.status is not ResolutionStatus.READY
            or not resolution.eligible_for_binding
            or resolution.selection.plan is None
            or resolution.validation is None
            or not resolution.validation.valid):
        raise RuntimeError(
            "Stage-4 resolver did not produce a validated optimum: "
            f"{resolution.status.value}")

    # Independent of the MILP, retained as a correctness oracle at this size.
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
    bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=(),
        scientific_snapshot_refs=(
            PlanSnapshotRef(
                "capability_catalog",
                expansion.augmented_catalog.catalog_id),
            PlanSnapshotRef(
                "transformation_expansion", expansion.expansion_id),
            PlanSnapshotRef(
                "feasible_hypergraph", resolution.hypergraph.graph_id),
        ),
    )
    site_by_invocation = dict(resolution.selection.deployment_choices)
    profile_by_id = {
        value.profile_id: value
        for value in expansion.augmented_catalog.execution_profiles
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
        name="stage4-explicit-transformation-demo",
        deployment_snapshot=fixture.deployment_snapshot,
        execution_profiles=expansion.augmented_catalog.execution_profiles,
        root_uses=fixture.root_uses,
        artifact_leaves=(),
    )
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None):
        raise RuntimeError(compilation.record.message)
    return Stage4DemoPlan(
        fixture=fixture,
        expansion=expansion,
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
            raise RuntimeError(f"Stage-4 compiled run failed: {run_state.value}")
        value = controller.output_value(run_id, task.task_id, root_binding[3])
        with controller.store.connect() as connection:
            attempts = int(connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0])

    transform_ids = sorted(
        value.capability_id for value in demo.selected_invocations
        if value.capability_id.startswith("transform:"))
    return {
        "schema": "stage4-demo-result-v1",
        "resolution_status": demo.resolution.status.value,
        "transformation_closure_complete": demo.expansion.complete,
        "effective_discovery_complete": (
            demo.resolution.selection.discovery_complete),
        "globally_optimal": (
            demo.resolution.selection.globally_optimal_over_discovery_space),
        "validation_passed": demo.resolution.validation.valid,
        "eligible_for_binding": demo.resolution.eligible_for_binding,
        "expansion_id": demo.expansion.expansion_id,
        "reachable_descriptor_states": len(
            demo.expansion.reachable_descriptor_ids),
        "selected_transformations": transform_ids,
        "selected_capabilities": sorted(
            value.capability_id for value in demo.selected_invocations),
        "selected_cost_units": demo.resolution.selection.objective_cost_units,
        "rejected_direct_alternative_cost": 9,
        "resolution_id": demo.resolution.resolution_id,
        "hypergraph_id": demo.resolution.hypergraph.graph_id,
        "candidate_plan_id": demo.resolution.selection.plan.plan_id,
        "bound_plan_id": demo.bound_plan.bound_plan_id,
        "deployment_plan_id": demo.deployment_plan.deployment_plan_id,
        "stage1_graph_id": graph.plan_id,
        "task_count": len(graph.tasks),
        "attempt_count": attempts,
        "source_metres": demo.fixture.expected_result * 1000,
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


__all__ = ["Stage4DemoPlan", "build_demo_plan", "run_demo"]
