"""Runnable Stage-6 dataset-versus-model demonstration.

Three requests run against one frozen graph:

1. **minimum cost** — the modelled path wins on cost and executes end to end;
2. **out of scope** — the same request in a region the model's evidence does
   not cover, where the model is inadmissible and the direct source wins; and
3. **quality** — which returns ``CHOICE_REQUIRED`` rather than a ranking, then
   honours an explicit human choice and binds that decision into the plan ID.

The third case is the point of the stage. The evidence here is comparable and
its confidence intervals do not overlap, so the system *could* pick a winner —
and it still refuses to, because an automatic empirical-quality optimizer is
not something this MVP can honestly claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from capabilities import BoundInvocation
from composition import CompilationResult, CompilationStatus, compile_bound_plan
from engine.runtime import RunState, WorkflowController
from objectives import (
    ChoiceRecord,
    ChoiceRequiredReport,
    ObjectiveOutcome,
    ObjectiveRequest,
    SelectionObjective,
    resolve_with_objective,
)
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
    SelectionConstraints,
    WorkflowResolver,
)

from . import fixtures as fx


@dataclass(frozen=True)
class Stage6DemoPlan:
    fixture: fx.Stage6Fixture
    resolution: ResolutionOutcome
    selected_invocations: tuple[BoundInvocation, ...]
    bound_plan: BoundDerivationPlan
    deployment_plan: DeploymentPlan
    compilation: CompilationResult


def _resolver(fixture: fx.Stage6Fixture) -> Callable[
        [SelectionConstraints], ResolutionOutcome]:
    def resolve(constraints: SelectionConstraints) -> ResolutionOutcome:
        return WorkflowResolver(
            fixture.catalog, fixture.deployment_snapshot,
            evidence_snapshot=fixture.evidence_snapshot,
        ).resolve(fixture.root_uses, constraints=constraints)
    return resolve


def _selected_capability_ids(outcome: ResolutionOutcome) -> list[str]:
    if outcome.selection.plan is None:
        return []
    chosen = set(outcome.selection.plan.selected_invocation_ids)
    return sorted(node.invocation.capability_id
                  for node in outcome.hypergraph.invocation_nodes
                  if node.invocation_id in chosen)


def build_demo_plan(fixture: fx.Stage6Fixture | None = None,
                    *, decision_ref: PlanSnapshotRef | None = None,
                    outcome: ResolutionOutcome | None = None,
                    ) -> Stage6DemoPlan:
    fixture = fixture or fx.make_stage6_fixture()
    resolution = outcome or _resolver(fixture)(SelectionConstraints())
    if (resolution.status is not ResolutionStatus.READY
            or resolution.selection.plan is None
            or resolution.validation is None
            or not resolution.validation.valid):
        raise RuntimeError(
            f"Stage-6 resolver did not validate: {resolution.status.value}")

    plan = resolution.selection.plan
    chosen = set(plan.selected_invocation_ids)
    selected = tuple(sorted(
        (node.invocation for node in resolution.hypergraph.invocation_nodes
         if node.invocation_id in chosen),
        key=lambda value: value.invocation_key))

    snapshot_refs = [
        PlanSnapshotRef("capability_catalog", fixture.catalog.catalog_id),
        PlanSnapshotRef("evidence", fixture.evidence_snapshot.snapshot_id),
        PlanSnapshotRef("feasible_hypergraph", resolution.hypergraph.graph_id),
    ]
    if decision_ref is not None:
        # The human decision is part of what this plan *is*, so it belongs in
        # the identity rather than in a side log.
        snapshot_refs.append(decision_ref)

    bound = BoundDerivationPlan.bind(
        plan,
        invocation_bindings=tuple(
            _bound_invocation_binding(value) for value in selected),
        artifact_bindings=(),
        scientific_snapshot_refs=tuple(snapshot_refs))
    site_by_invocation = dict(resolution.selection.deployment_choices)
    profile_by_id = {value.profile_id: value
                     for value in fixture.catalog.execution_profiles}
    deployment = DeploymentPlan.bind(
        bound,
        deployment_snapshot_ref=PlanSnapshotRef(
            "deployment", fixture.deployment_snapshot.snapshot_id),
        invocation_bindings=tuple(
            _deployment_binding(
                value, site_by_invocation[value.invocation_key],
                profile_by_id[value.execution_profile_id])
            for value in selected))
    compilation = compile_bound_plan(
        bound, selected, deployment_plan=deployment,
        name="stage6-dataset-versus-model-demo",
        deployment_snapshot=fixture.deployment_snapshot,
        execution_profiles=fixture.catalog.execution_profiles,
        root_uses=fixture.root_uses, artifact_leaves=(),
        evidence_snapshot=fixture.evidence_snapshot)
    if (compilation.record.status is not CompilationStatus.COMPILED
            or compilation.graph is None):
        raise RuntimeError(compilation.record.message)
    return Stage6DemoPlan(fixture, resolution, selected, bound, deployment,
                          compilation)


def quality_request(fixture: fx.Stage6Fixture) -> ObjectiveOutcome:
    """Ask for the best-quality source and receive a decision request."""
    return resolve_with_objective(
        _resolver(fixture),
        ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY),
        concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot,
        )


def apply_choice(fixture: fx.Stage6Fixture, report: ChoiceRequiredReport,
                 capability_id: str) -> ObjectiveOutcome:
    """Honour a human's versioned choice of one presented alternative."""
    alternative = next(item for item in report.alternatives
                       if item.capability_id == capability_id)
    choice = ChoiceRecord(
        report_id=report.report_id,
        chosen_producer=alternative.producer,
        choice_version="stage6-demo-choice-v1",
        rationale=(
            "operator preferred the directly observed source over the modelled "
            "one despite its higher measured error and higher cost, because "
            "the model's applicability was judged too narrow for this study"))
    return resolve_with_objective(
        _resolver(fixture),
        ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY, choice=choice),
        concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot,
        )


def run_demo(runtime_root: Path | str) -> dict[str, Any]:
    root = Path(runtime_root).resolve()
    fixture = fx.make_stage6_fixture()

    # -- 1. minimum cost, executed ---------------------------------------
    demo = build_demo_plan(fixture)
    graph = demo.compilation.graph
    assert graph is not None
    root_binding = demo.compilation.record.root_bindings[0]
    if root_binding[1] != "TASK_OUTPUT":
        raise RuntimeError("demo root did not compile to an executable output")
    task = graph.task_by_key(root_binding[2])
    with WorkflowController(root / "runtime") as controller:
        run_id = controller.create_run(graph)
        run_state = controller.run_until_terminal(run_id, timeout_s=120)
        if run_state is not RunState.SUCCEEDED:
            raise RuntimeError(f"Stage-6 run failed: {run_state.value}")
        value = controller.output_value(run_id, task.task_id, root_binding[3])
        with controller.store.connect() as connection:
            attempts = int(connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0])

    # -- 2. same request where the model's evidence does not apply -------
    out_of_scope = fx.make_stage6_fixture(
        model_applicability_bounds=fx.OUT_OF_SCOPE_BOUNDS)
    gated = _resolver(out_of_scope)(SelectionConstraints())

    # -- 3. quality request, then an explicit choice ---------------------
    quality = quality_request(fixture)
    if not quality.choice_required:
        raise RuntimeError("a quality request must return CHOICE_REQUIRED")
    chosen = apply_choice(fixture, quality.report, "example-flow-direct")
    chosen_plan = build_demo_plan(
        fixture, decision_ref=chosen.snapshot_ref, outcome=chosen.resolution)
    undecided_plan = build_demo_plan(fixture)

    comparability = quality.report.comparability
    return {
        "schema": "stage6-demo-result-v1",
        "minimum_cost": {
            "resolution_status": demo.resolution.status.value,
            "globally_optimal": (
                demo.resolution.selection.globally_optimal_over_discovery_space),
            "selected_capabilities": _selected_capability_ids(demo.resolution),
            "cost_units": demo.resolution.selection.objective_cost_units,
            "rejected_direct_path_cost": fx.DIRECT_PATH_TOTAL,
            "task_count": len(graph.tasks),
            "attempt_count": attempts,
            "run_state": run_state.value,
            "result_matches_expected": value == fx.expected_result_field(),
            "result_sample": value["components"]["flow"][0][0][0],
        },
        "evidence_gating": {
            "resolution_status": gated.status.value,
            "selected_capabilities": _selected_capability_ids(gated),
            "cost_units": gated.selection.objective_cost_units,
            "model_excluded_where_evidence_does_not_apply": (
                "example-flow-model" not in _selected_capability_ids(gated)),
        },
        "quality_request": {
            "status": quality.status.value,
            "ranking_complete": quality.report.ranking_complete,
            "nondominance_claimed": quality.report.nondominance_claimed,
            "report_id": quality.report.report_id,
            "alternatives": [
                {
                    "capability_id": item.capability_id,
                    "admissible": item.admissible,
                    "cost_units": item.cost_units,
                    "metric_value": (
                        item.reading.value if item.reading else None),
                    "applicable": (
                        item.reading.applicability_covers_request
                        if item.reading else None),
                }
                for item in quality.report.alternatives
            ],
            "comparable": comparability.comparable,
            "blocking_codes": [
                code.value for code in comparability.blocking_codes],
            "intervals_overlap": comparability.intervals_overlap,
            "separation_established": comparability.separation_established,
            "auto_selected": False,
        },
        "recorded_choice": {
            "status": chosen.status.value,
            "decision_id": chosen.decision_id,
            "selected_capabilities": _selected_capability_ids(chosen.resolution),
            "cost_units": chosen.resolution.selection.objective_cost_units,
            "bound_plan_id_with_decision": chosen_plan.bound_plan.bound_plan_id,
            "bound_plan_id_without_decision": (
                undecided_plan.bound_plan.bound_plan_id),
            "decision_changes_plan_identity": (
                chosen_plan.bound_plan.bound_plan_id
                != undecided_plan.bound_plan.bound_plan_id),
        },
        "planning_metrics_ns": demo.resolution.metrics.to_dict(),
    }


def _bound_invocation_binding(
        invocation: BoundInvocation) -> BoundInvocationBinding:
    component = invocation.implementation.verify_current()
    return BoundInvocationBinding(
        invocation_id=invocation.invocation_key,
        component_id=component.component_id,
        component_version=component.version,
        implementation_digest=component.implementation_digest,
        operation_key=component.operation_key,
        runtime_parameters=dict(invocation.parameters))


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
        })


__all__ = ["Stage6DemoPlan", "apply_choice", "build_demo_plan",
           "quality_request", "run_demo"]
