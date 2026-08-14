from __future__ import annotations

import json
from dataclasses import replace

import pytest

from capabilities import artifact_evidence_subject
from composition import OracleStatus, exhaustive_enumerate
from contracts import (
    EvidenceProfile,
    EvidenceRequirement,
    EvidenceSnapshot,
    EvidenceSubject,
    MetricEvaluator,
    RequirementUse,
)
from plans import ProducerKind
from resolution import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitRecord,
    ArtifactCommitStatus,
    DiscoveryLimits,
    MilpSolveOptions,
    ProducerSelectionRef,
    PlanningBenchmarkProfile,
    ResolutionStatus,
    SelectionConstraints,
    WorkflowResolver,
)
from stage3.demo import build_demo_plan, run_demo
from stage3.fixtures import make_composition_fixture


def _resolver(*, limits: DiscoveryLimits = DiscoveryLimits()):
    fixture = make_composition_fixture()
    availability = ArtifactAvailabilitySnapshot.freeze(
        ArtifactCommitRecord(value.leaf_id, ArtifactCommitStatus.COMMITTED)
        for value in fixture.offered_artifact_leaves)
    return fixture, WorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        artifact_leaves=fixture.offered_artifact_leaves,
        availability_snapshot=availability,
        discovery_limits=limits,
    )


def test_domain_neutral_resolver_discovers_selects_validates_and_executes(tmp_path):
    result = run_demo(tmp_path / "runtime")

    assert result["resolution_status"] == "READY"
    assert result["discovery_complete"] is True
    assert result["solver_status"] == "OPTIMAL"
    assert result["globally_optimal"] is True
    assert result["validation_passed"] is True
    assert result["eligible_for_binding"] is True
    assert result["selected_capabilities"] == ["example-add", "example-pair"]
    assert result["selected_cost_units"] == 3
    assert result["invocations_discovered"] == 4
    assert result["requirements_discovered"] == 3
    assert result["uses_preserved"] == 3
    assert result["task_count"] == 2
    assert result["attempt_count"] == 2
    assert result["result"] == 42
    assert result["run_state"] == "SUCCEEDED"


def test_production_milp_selection_matches_independent_exhaustive_oracle():
    _fixture, resolver = _resolver()
    outcome = resolver.resolve(_fixture.root_uses)
    oracle = exhaustive_enumerate(outcome.selector_problem)

    assert oracle.status is OracleStatus.OPTIMAL
    assert outcome.selection.plan is not None
    assert outcome.selection.plan.plan_id == oracle.optimal_plan.plan_id


def test_resolution_outcome_serializes_complete_selection_diagnostics():
    fixture, resolver = _resolver()
    outcome = resolver.resolve(fixture.root_uses)
    result = outcome.selection

    first = outcome.to_dict()
    second = outcome.to_dict()
    assert first == second
    # The public payload must be accepted by strict JSON serialization and
    # have a stable canonical representation for the same immutable outcome.
    assert json.dumps(
        first, allow_nan=False, sort_keys=True, separators=(",", ":")
    ) == json.dumps(
        second, allow_nan=False, sort_keys=True, separators=(",", ":")
    )

    assert first["selection"] == {
        "selection_problem_id": result.selection_problem_id,
        "graph_problem_id": result.graph_problem_id,
        "status": result.status.value,
        "discovery_complete": result.discovery_complete,
        "primary_cost_proven": result.primary_cost_proven,
        "tie_break_complete": result.tie_break_complete,
        "globally_optimal": result.globally_optimal_over_discovery_space,
        "plan_id": result.plan.plan_id if result.plan is not None else None,
        "deployment_choices": [
            list(value) for value in result.deployment_choices
        ],
        "objective_cost_units": result.objective_cost_units,
        "solver_status": result.solver_status,
        "mip_gap": result.mip_gap,
        "mip_node_count": result.mip_node_count,
        "solver_calls": result.solver_calls,
        "solve_seconds": result.solve_seconds,
        "solver_message": result.solver_message,
        "blockers": [value.to_dict() for value in result.blockers],
    }
    assert first["selection"]["deployment_choices"] == sorted(
        first["selection"]["deployment_choices"]
    )


def test_explicit_exclusion_forces_the_more_expensive_non_pair_alternative():
    fixture, resolver = _resolver()
    baseline = resolver.resolve(fixture.root_uses)
    pair_id = next(
        value.invocation_id for value in baseline.hypergraph.invocation_nodes
        if value.invocation.capability_id == "example-pair")
    leaf_id = fixture.offered_artifact_leaves[0].leaf_id
    alternative = resolver.resolve(
        fixture.root_uses,
        constraints=SelectionConstraints.bind(exclude=(
            ProducerSelectionRef(ProducerKind.INVOCATION, pair_id),
            ProducerSelectionRef(ProducerKind.ARTIFACT_LEAF, leaf_id),
        )),
    )

    assert alternative.status is ResolutionStatus.READY
    assert alternative.validation is not None and alternative.validation.valid
    assert alternative.selection.objective_cost_units == 9
    selected = {
        value.invocation.capability_id
        for value in alternative.hypergraph.invocation_nodes
        if value.invocation_id in alternative.selection.plan.selected_invocation_ids
    }
    assert selected == {
        "example-add", "example-left-constant", "example-right-constant"}


def test_incomplete_discovery_is_never_reported_globally_optimal_or_executable():
    fixture, resolver = _resolver(
        limits=DiscoveryLimits(max_candidates=5))
    outcome = resolver.resolve(fixture.root_uses, require_proven_optimal=True)

    assert not outcome.hypergraph.discovery_complete
    assert not outcome.selection.globally_optimal_over_discovery_space
    assert outcome.status in {
        ResolutionStatus.FEASIBLE_NOT_PROVEN_OPTIMAL,
        ResolutionStatus.INCOMPLETE,
    }
    assert not outcome.eligible_for_binding


def test_empty_truncated_frontier_reports_incomplete_not_unsatisfiable():
    fixture, resolver = _resolver(limits=DiscoveryLimits(max_depth=0))

    outcome = resolver.resolve(fixture.root_uses)

    assert not outcome.hypergraph.discovery_complete
    assert outcome.selection.status.value == "UNSATISFIABLE"
    assert outcome.status is ResolutionStatus.INCOMPLETE
    assert not outcome.eligible_for_binding


def test_selected_artifact_is_validated_against_bound_availability_snapshot():
    fixture, resolver = _resolver()
    add = next(value for value in fixture.catalog.capabilities
               if value.capability_id == "example-add")
    left_requirement = next(value.requirement for value in add.input_ports
                            if value.port_id == "left")
    root = RequirementUse("stage3-artifact-root", "artifact", left_requirement)

    outcome = resolver.resolve((root,))

    assert outcome.status is ResolutionStatus.READY
    assert outcome.validation is not None and outcome.validation.valid
    assert outcome.selection.plan.selected_artifact_leaf_ids == (
        fixture.offered_artifact_leaves[0].leaf_id,)
    assert outcome.eligible_for_binding


def test_artifact_evidence_is_bound_to_exact_manifest_realization():
    fixture = make_composition_fixture()
    base_leaf = fixture.offered_artifact_leaves[0]
    add = next(value for value in fixture.catalog.capabilities
               if value.capability_id == "example-add")
    requirement = replace(
        next(value.requirement for value in add.input_ports
             if value.port_id == "left"),
        minimum_evidence=EvidenceRequirement(
            required_profile_schema="SyntheticEvidence-v1",
            allow_unknown_empirical=True,
        ),
    )
    root = RequirementUse("stage3-evidence-root", "artifact", requirement)
    unrelated = EvidenceProfile(
        "SyntheticEvidence-v1",
        EvidenceSubject(
            "unrelated-component", "9.9", "unrelated-config", "other"),
        (),
    )
    wrong_leaf = replace(
        base_leaf, evidence_profile_id=unrelated.profile_id)
    wrong_snapshot = EvidenceSnapshot(
        "2026-08-13T00:00:00Z",
        MetricEvaluator("synthetic-empty", "1", ()),
        (unrelated,),
    )
    wrong_availability = ArtifactAvailabilitySnapshot.freeze((
        ArtifactCommitRecord(
            wrong_leaf.leaf_id, ArtifactCommitStatus.COMMITTED),
    ))
    wrong = WorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        artifact_leaves=(wrong_leaf,),
        availability_snapshot=wrong_availability,
        evidence_snapshot=wrong_snapshot,
    ).resolve((root,))

    assert wrong.status is ResolutionStatus.UNSATISFIABLE
    assert not wrong.hypergraph.satisfaction_arcs
    assert any(
        "EVIDENCE_SUBJECT_MISMATCH" in rejection.codes
        for rejection in wrong.hypergraph.rejections)

    correct = EvidenceProfile(
        "SyntheticEvidence-v1",
        artifact_evidence_subject(base_leaf),
        (),
    )
    correct_leaf = replace(base_leaf, evidence_profile_id=correct.profile_id)
    correct_snapshot = EvidenceSnapshot(
        "2026-08-13T00:00:00Z",
        MetricEvaluator("synthetic-empty", "1", ()),
        (correct,),
    )
    correct_availability = ArtifactAvailabilitySnapshot.freeze((
        ArtifactCommitRecord(
            correct_leaf.leaf_id, ArtifactCommitStatus.COMMITTED),
    ))
    accepted = WorkflowResolver(
        fixture.catalog,
        fixture.deployment_snapshot,
        artifact_leaves=(correct_leaf,),
        availability_snapshot=correct_availability,
        evidence_snapshot=correct_snapshot,
    ).resolve((root,))

    assert accepted.status is ResolutionStatus.READY
    assert accepted.validation is not None and accepted.validation.valid
    assert accepted.eligible_for_binding


def test_warm_planning_p95_is_below_conformance_regression_target():
    fixture, resolver = _resolver()
    solve_options = MilpSolveOptions(time_limit_s=30.0, presolve=False)
    profile = PlanningBenchmarkProfile.measure(
        lambda: resolver.resolve(
            fixture.root_uses, solve_options=solve_options),
        warmup_runs=1,
        measured_runs=30,
        solve_options=solve_options,
        run_config={
            "fixture": "domain-neutral-pair-add",
            "process_state": "warm",
        },
    )

    assert profile.measured_runs == 30
    assert profile.benchmark_scope == "STAGE3_CONFORMANCE_MICROBENCHMARK"
    assert profile.slo_interpretation == "NOT_A_STAGE6_REPRESENTATIVE_SLO"
    assert profile.timing_source == (
        "PERF_COUNTER_NS_AROUND_FULL_RESOLVE_CALL")
    assert profile.resolution_status == "READY"
    assert profile.selection_status == "OPTIMAL"
    assert profile.discovery_complete
    assert profile.validation_passed
    assert profile.globally_optimal
    assert profile.eligible_for_binding
    assert profile.graph_id
    assert profile.selector_problem_id
    assert profile.selection_problem_id
    assert profile.selected_plan_id
    assert profile.validation_report_id
    assert profile.invocation_count == 4
    assert profile.satisfaction_arc_count == 6
    assert profile.cpu_model
    assert profile.memory_total_bytes is None or profile.memory_total_bytes > 0
    assert profile.solver_name == "scipy.optimize.milp"
    assert profile.solver_version == profile.scipy_version
    assert profile.highs_version
    assert profile.scipy_version
    assert profile.numpy_version
    assert profile.solve_options["time_limit_s"] == 30.0
    assert profile.run_config["fixture"] == "domain-neutral-pair-add"
    assert len(profile.total_ns_samples) == 30
    assert len(profile.resolver_total_ns_samples) == 30
    assert all(external >= internal for external, internal in zip(
        profile.total_ns_samples, profile.resolver_total_ns_samples))
    assert profile.p95_total_ns < 5_000_000_000
    record = profile.to_dict()
    assert record["benchmark_id"] == profile.benchmark_id
    assert record["schema"] == "stage3-planning-benchmark-profile-v2"
    assert PlanningBenchmarkProfile.from_dict(
        json.loads(json.dumps(record))) == profile

    tampered = profile.to_dict()
    tampered["total_ns_samples"][0] += 1
    with pytest.raises(ValueError, match="p95|identity"):
        PlanningBenchmarkProfile.from_dict(tampered)


def test_planning_benchmark_rejects_nonready_resolution_outcomes():
    fixture, resolver = _resolver(limits=DiscoveryLimits(max_depth=0))

    with pytest.raises(ValueError, match="did not return READY"):
        PlanningBenchmarkProfile.measure(
            lambda: resolver.resolve(fixture.root_uses),
            warmup_runs=0,
            measured_runs=1,
        )


def test_planning_benchmark_rejects_changing_graph_or_plan_context():
    fixture, resolver = _resolver()
    baseline = resolver.resolve(fixture.root_uses)
    pair_id = next(
        value.invocation_id for value in baseline.hypergraph.invocation_nodes
        if value.invocation.capability_id == "example-pair")
    leaf_id = fixture.offered_artifact_leaves[0].leaf_id
    alternative_constraints = SelectionConstraints.bind(exclude=(
        ProducerSelectionRef(ProducerKind.INVOCATION, pair_id),
        ProducerSelectionRef(ProducerKind.ARTIFACT_LEAF, leaf_id),
    ))
    calls = 0

    def changing_resolve():
        nonlocal calls
        calls += 1
        return resolver.resolve(
            fixture.root_uses,
            constraints=(None if calls == 1 else alternative_constraints),
        )

    with pytest.raises(ValueError, match="changing graph/plan context"):
        PlanningBenchmarkProfile.measure(
            changing_resolve,
            warmup_runs=0,
            measured_runs=2,
        )


def test_stage3_compilation_keeps_one_task_for_two_output_invocation():
    demo = build_demo_plan()
    graph = demo.compilation.graph

    assert graph is not None
    assert len(graph.tasks) == 2
    pair = next(value for value in demo.selected_invocations
                if value.capability_id == "example-pair")
    mapping = dict(demo.compilation.record.invocation_task_keys)
    pair_task = graph.task_by_key(mapping[pair.invocation_key])
    assert {value.output_name for value in pair_task.outputs} == {"left", "right"}
