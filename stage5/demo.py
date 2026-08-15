"""Runnable Stage-5 progressive-acquisition demonstration.

The chain is deliberately staged so the ordering invariant is *measured*, not
asserted:

1. metadata discovery only — connectors are asked what exists, and the byte
   counters must still read zero when the availability snapshot freezes;
2. catalog construction, transformation closure, global selection, and
   independent validation — still zero bytes;
3. binding verification and payload transfer — the first bytes move here, and
   only for assets a manifest already bound; then
4. Stage-1 compilation, durable execution, and validated commit.

The contest itself is the point.  A pinned local artifact already in the
requested units costs 8.  A remote archive holds two coarse tiles in different
units at cost 3, which needs a declared cost-2 conversion.  The resolver has to
prefer 5 over 8 while proving the two tiles actually cover the request.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acquisition import (
    AcquisitionExpansion,
    AcquisitionLimits,
    AcquisitionSearch,
    BoundAssetManifest,
    ManifestShardStore,
    PayloadFetcher,
    PayloadStore,
    PlanningSessionStore,
    ProviderQuota,
    lower_manifest_to_capability,
    verify_binding,
)
from capabilities import BoundInvocation, CapabilityCatalog
from composition import CompilationResult, CompilationStatus, compile_bound_plan
from contracts import OriginClass
from contracts.identity import decimal_value
from engine.runtime import RunState, WorkflowController
from engine.runtime.operations import ASSET_STORE_ENVIRONMENT
from plans import (
    BoundDerivationPlan,
    BoundInvocationBinding,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
)
from resolution import (
    ResolutionOutcome,
    ResolutionStatus,
    UpstreamCompleteness,
    WorkflowResolver,
)
from transformations import (
    TransformationSearchLimits,
    expand_transform_catalog,
)

from . import fixtures as fx


@dataclass(frozen=True)
class Stage5DemoPlan:
    acquisition: AcquisitionExpansion
    coarse_bound: BoundAssetManifest
    local_bound: BoundAssetManifest
    support_bound: BoundAssetManifest
    catalog: CapabilityCatalog
    upstream: UpstreamCompleteness
    resolution: ResolutionOutcome
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation: CompilationResult
    planning_bytes: int


def _union_bounds(bound: BoundAssetManifest,
                  shards: ManifestShardStore) -> tuple[str, str, str, str]:
    """The extent the bound assets *actually* cover, not the one requested."""
    selected = set(bound.asset_ids)
    boxes = [tuple(decimal_value(value)
                   for value in asset.extent.spatial.bounds)
             for asset in bound.manifest.iter_assets(shards)
             if asset.asset_id in selected]
    return (str(min(item[0] for item in boxes)),
            str(min(item[1] for item in boxes)),
            str(max(item[2] for item in boxes)),
            str(max(item[3] for item in boxes)))


def build_demo_plan(root: Path, *, session_id: str = "stage5-demo") -> Stage5DemoPlan:
    session_store = PlanningSessionStore(root / "planning.sqlite3")
    shards = ManifestShardStore(root / "manifest-shards")
    local = fx.make_local_connector()
    remote = fx.make_remote_connector()

    # -- phase 1: metadata only ------------------------------------------
    acquisition = AcquisitionSearch(
        session_store, shards, (local, remote),
        limits=AcquisitionLimits(), quota=ProviderQuota(),
    ).discover(session_id, fx.acquisition_requests(),
               second_order=(fx.support_rule(),))
    planning_bytes = local.bytes_transferred + remote.bytes_transferred
    if planning_bytes:
        raise RuntimeError(
            "metadata discovery transferred payload bytes; the Stage-5 "
            "ordering invariant is broken")

    coarse = acquisition.manifest_for(fx.remote_query().query_id)
    pinned = acquisition.manifest_for(fx.local_query().query_id)
    support = next(
        (item for item in acquisition.bound_manifests
         if item is not coarse and item is not pinned), None)
    if coarse is None or pinned is None or support is None:
        raise RuntimeError("Stage-5 discovery did not bind all three manifests")

    # -- phase 2: catalog, closure, selection, validation ----------------
    coarse_descriptor = fx.descriptor(
        fx.FLOW_CONCEPT, fx.COARSE_UNITS, _union_bounds(coarse, shards),
        OriginClass.OBSERVATION)
    converted_descriptor = fx.descriptor(
        fx.FLOW_CONCEPT, fx.TARGET_UNITS, _union_bounds(coarse, shards),
        OriginClass.DERIVED)
    pinned_descriptor = fx.descriptor(
        fx.FLOW_CONCEPT, fx.TARGET_UNITS, _union_bounds(pinned, shards),
        OriginClass.OBSERVATION)
    support_descriptor = fx.descriptor(
        fx.SUPPORT_CONCEPT, fx.SUPPORT_UNITS, _union_bounds(support, shards),
        OriginClass.OBSERVATION)

    profile = fx.acquisition_profile()
    base_catalog = CapabilityCatalog.freeze(
        (
            lower_manifest_to_capability(
                coarse, coarse_descriptor, profile, cost_units=fx.REMOTE_COST),
            lower_manifest_to_capability(
                pinned, pinned_descriptor, profile, cost_units=fx.LOCAL_COST),
            lower_manifest_to_capability(
                support, support_descriptor, profile,
                cost_units=fx.SUPPORT_COST),
            fx.downscale_model(
                coarse_descriptor, support_descriptor, converted_descriptor),
        ),
        (profile, fx.transform_profile(), fx.model_profile()),
    )
    closure = expand_transform_catalog(
        base_catalog,
        (fx.unit_transform(coarse_descriptor, converted_descriptor),),
        limits=TransformationSearchLimits())

    # Two discovery layers ran above the resolver.  They are folded into the
    # single upstream channel Stage 4 introduced rather than reported twice.
    upstream = UpstreamCompleteness.merge_all((
        UpstreamCompleteness.from_layer(
            acquisition.complete, acquisition.limit_codes),
        UpstreamCompleteness.from_layer(
            closure.complete,
            tuple(sorted({item.code.value for item in closure.limit_reasons}))),
    ))
    resolution = WorkflowResolver(
        closure.augmented_catalog,
        fx.deployment_snapshot(),
        **upstream.resolver_kwargs(),
    ).resolve(fx.root_uses())
    if (resolution.status is not ResolutionStatus.READY
            or not resolution.eligible_for_binding
            or resolution.selection.plan is None
            or resolution.validation is None
            or not resolution.validation.valid):
        raise RuntimeError(
            f"Stage-5 resolver did not produce a validated optimum: "
            f"{resolution.status.value}")

    plan = resolution.selection.plan
    selected_ids = set(plan.selected_invocation_ids)
    selected = tuple(sorted(
        (value.invocation for value in resolution.hypergraph.invocation_nodes
         if value.invocation_id in selected_ids),
        key=lambda value: value.invocation_key))
    bound_plan = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=(),
        scientific_snapshot_refs=(
            PlanSnapshotRef("acquisition_expansion", acquisition.expansion_id),
            PlanSnapshotRef("capability_catalog",
                            closure.augmented_catalog.catalog_id),
            PlanSnapshotRef("transformation_expansion", closure.expansion_id),
            PlanSnapshotRef("feasible_hypergraph", resolution.hypergraph.graph_id),
        ),
    )
    site_by_invocation = dict(resolution.selection.deployment_choices)
    profile_by_id = {
        value.profile_id: value
        for value in closure.augmented_catalog.execution_profiles}
    deployment_plan = DeploymentPlan.bind(
        bound_plan,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", fx.deployment_snapshot().snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(
                value, site_by_invocation[value.invocation_key],
                profile_by_id[value.execution_profile_id])
            for value in selected),
    )
    compilation = compile_bound_plan(
        bound_plan, selected,
        deployment_plan=deployment_plan,
        name="stage5-progressive-acquisition-demo",
        deployment_snapshot=fx.deployment_snapshot(),
        execution_profiles=closure.augmented_catalog.execution_profiles,
        root_uses=fx.root_uses(),
        artifact_leaves=(),
    )
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None):
        raise RuntimeError(compilation.record.message)

    planning_bytes = local.bytes_transferred + remote.bytes_transferred
    if planning_bytes:
        raise RuntimeError(
            "planning transferred payload bytes before binding was complete")
    return Stage5DemoPlan(
        acquisition=acquisition,
        coarse_bound=coarse,
        local_bound=pinned,
        support_bound=support,
        catalog=closure.augmented_catalog,
        upstream=upstream,
        resolution=resolution,
        selected_invocations=selected,
        bound_plan=bound_plan,
        deployment_plan=deployment_plan,
        compilation=compilation,
        planning_bytes=planning_bytes,
    )


def run_demo(runtime_root: Path | str) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    planning_root = root / "planning"
    planning_root.mkdir(parents=True, exist_ok=True)
    demo = build_demo_plan(planning_root)

    shards = ManifestShardStore(planning_root / "manifest-shards")
    session_store = PlanningSessionStore(planning_root / "planning.sqlite3")
    payloads = PayloadStore(root / "assets")
    remote = fx.make_remote_connector()

    # -- phase 3: verify the binding, then move bytes --------------------
    verification = verify_binding(
        demo.coarse_bound, shards, remote.observed_identities())
    if not verification.fresh:
        raise RuntimeError("the bound coarse manifest went stale before transfer")
    receipt = PayloadFetcher(
        session_store, payloads, quota=ProviderQuota()
    ).fetch(demo.coarse_bound, remote, shards)
    if remote.bytes_transferred <= 0:
        raise RuntimeError("payload transfer moved no bytes")

    # -- phase 4: execute the compiled plan ------------------------------
    graph = demo.compilation.graph
    assert graph is not None
    root_binding = demo.compilation.record.root_bindings[0]
    if root_binding[1] != "TASK_OUTPUT":
        raise RuntimeError("demo root did not compile to an executable output")
    task = graph.task_by_key(root_binding[2])
    previous = os.environ.get(ASSET_STORE_ENVIRONMENT)
    os.environ[ASSET_STORE_ENVIRONMENT] = str(payloads.root)
    try:
        with WorkflowController(root / "runtime") as controller:
            run_id = controller.create_run(graph)
            run_state = controller.run_until_terminal(run_id, timeout_s=60)
            if run_state is not RunState.SUCCEEDED:
                raise RuntimeError(f"Stage-5 compiled run failed: {run_state.value}")
            value = controller.output_value(
                run_id, task.task_id, root_binding[3])
            with controller.store.connect() as connection:
                attempts = int(connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
                ).fetchone()[0])
    finally:
        if previous is None:
            os.environ.pop(ASSET_STORE_ENVIRONMENT, None)
        else:
            os.environ[ASSET_STORE_ENVIRONMENT] = previous

    expected = fx.expected_converted_field()
    return {
        "schema": "stage5-demo-result-v1",
        "discovery_rounds": demo.acquisition.rounds,
        "second_order_query_ran": demo.acquisition.rounds > 1,
        "acquisition_complete": demo.acquisition.complete,
        "bound_manifest_count": len(demo.acquisition.bound_manifests),
        "coarse_manifest_root": demo.coarse_bound.manifest_root,
        "coarse_asset_ids": list(demo.coarse_bound.asset_ids),
        "coverage_status": demo.coarse_bound.coverage.status.value,
        "bytes_transferred_during_planning": demo.planning_bytes,
        "bytes_transferred_after_binding": remote.bytes_transferred,
        "binding_status": verification.status.value,
        "fetch_receipt_assets": list(receipt.asset_ids),
        "upstream_completeness": demo.upstream.to_dict(),
        "effective_discovery_complete": (
            demo.resolution.selection.discovery_complete),
        "globally_optimal": (
            demo.resolution.selection.globally_optimal_over_discovery_space),
        "resolution_status": demo.resolution.status.value,
        "validation_passed": demo.resolution.validation.valid,
        "selected_capabilities": sorted(
            value.capability_id for value in demo.selected_invocations),
        "selected_cost_units": demo.resolution.selection.objective_cost_units,
        "rejected_local_alternative_cost": fx.LOCAL_COST,
        "manifest_root_in_bound_plan": _manifest_root_is_bound(demo),
        "resolution_id": demo.resolution.resolution_id,
        "bound_plan_id": demo.bound_plan.bound_plan_id,
        "deployment_plan_id": demo.deployment_plan.deployment_plan_id,
        "stage1_graph_id": graph.plan_id,
        "task_count": len(graph.tasks),
        "attempt_count": attempts,
        "result_matches_expected": value == expected,
        "result_sample": value["components"]["speed"][0][0],
        "run_state": run_state.value,
        "planning_metrics_ns": demo.resolution.metrics.to_dict(),
    }


def _manifest_root_is_bound(demo: Stage5DemoPlan) -> bool:
    """The bound derivation must carry the exact manifest it reads."""
    return any(
        binding.runtime_parameters.get("manifest_root")
        == demo.coarse_bound.manifest_root
        for binding in demo.bound_plan.invocation_bindings)


def _bound_invocation_binding(
        invocation: BoundInvocation) -> BoundInvocationBinding:
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
            "walltime_s": 60,
        },
    )


__all__ = ["Stage5DemoPlan", "build_demo_plan", "run_demo"]
