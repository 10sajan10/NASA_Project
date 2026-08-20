"""Stage-6 end to end: dataset versus model, evidence gating, human choice.

The exit gates under test are that direct and model-produced sources compete in
one global selector, that the model is admissible only where its evidence
applies, that a quality request never auto-selects, and that a recorded choice
changes what the plan *is*.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from objectives import (
    ChoiceRecord,
    ObjectiveRequest,
    ObjectiveStatus,
    SelectionObjective,
    StaleChoiceError,
    build_choice_report,
    resolve_with_objective,
)
from resolution import (
    DiscoveryCertificate,
    DiscoveryUniverseContract,
    ResolutionStatus,
    SelectionConstraints,
    WorkflowResolver,
)
from stage6 import fixtures as fx
from stage6.demo import (
    apply_choice,
    build_demo_plan,
    quality_request,
    run_demo,
)


def _resolve(fixture, constraints=None):
    return WorkflowResolver(
        fixture.catalog, fixture.deployment_snapshot,
        discovery_certificate=DiscoveryCertificate.for_base_catalog(
            fixture.catalog),
        discovery_universe=DiscoveryUniverseContract.declare(
            fixture.catalog.catalog_id),
        evidence_snapshot=fixture.evidence_snapshot,
    ).resolve(fixture.root_uses, constraints=constraints or SelectionConstraints())


def _selected(outcome):
    if outcome.selection.plan is None:
        return []
    chosen = set(outcome.selection.plan.selected_invocation_ids)
    return sorted(node.invocation.capability_id
                  for node in outcome.hypergraph.invocation_nodes
                  if node.invocation_id in chosen)


# -- the contest ----------------------------------------------------------


def test_direct_and_model_sources_compete_in_one_global_selection():
    fixture = fx.make_stage6_fixture()
    outcome = _resolve(fixture)

    assert outcome.status is ResolutionStatus.READY
    assert outcome.validation is not None and outcome.validation.valid
    assert outcome.selection.globally_optimal_over_discovery_space
    # The modelled path wins on cost against a real direct alternative.
    assert outcome.selection.objective_cost_units == fx.MINIMUM_COST_TOTAL
    assert "example-flow-model" in _selected(outcome)
    assert "example-flow-direct" not in _selected(outcome)


def test_compiled_stage6_fields_use_semantic_v2_validation():
    demo = build_demo_plan(fx.make_stage6_fixture())
    graph = demo.compilation.graph
    assert graph is not None
    semantic = [
        output.validation
        for task in graph.tasks
        for output in task.outputs
        if output.validation.get("kind") == "field_json_v2"
    ]
    # Coarse flow, terrain, modelled flow, and consequence output are all
    # load-bearing field artifacts. Scalar fuel/ignition remain finite JSON.
    assert len(semantic) == 4
    assert all(tuple(value["grid_shape"]) == (3, 3) for value in semantic)


def test_shared_terrain_input_is_counted_once():
    fixture = fx.make_stage6_fixture()
    outcome = _resolve(fixture)
    selected = _selected(outcome)
    # Terrain feeds both the consequence model and the downscaling model.
    assert selected.count("example-terrain") == 1
    assert fx.MINIMUM_COST_TOTAL == 11


def test_the_model_is_inadmissible_where_its_evidence_does_not_apply():
    fixture = fx.make_stage6_fixture(
        model_applicability_bounds=fx.OUT_OF_SCOPE_BOUNDS)
    outcome = _resolve(fixture)

    assert outcome.status is ResolutionStatus.READY
    # The cheaper modelled path exists and is still rejected, purely because
    # its evidence does not cover the requested region.
    assert "example-flow-model" not in _selected(outcome)
    assert "example-flow-direct" in _selected(outcome)
    assert outcome.selection.objective_cost_units == fx.DIRECT_PATH_TOTAL


def test_the_out_of_scope_model_never_becomes_a_candidate_at_all():
    """Inapplicable evidence is rejected at discovery, not merely deselected.

    This is stronger than losing on cost: the modelled producer does not enter
    the graph, so no constraint or objective could resurrect it.
    """
    in_scope = fx.make_stage6_fixture()
    assert any(node.invocation.capability_id == "example-flow-model"
               for node in _resolve(in_scope).hypergraph.invocation_nodes)

    out_of_scope = fx.make_stage6_fixture(
        model_applicability_bounds=fx.OUT_OF_SCOPE_BOUNDS)
    graph = _resolve(out_of_scope).hypergraph
    assert not any(node.invocation.capability_id == "example-flow-model"
                   for node in graph.invocation_nodes)
    # And the omission is recorded rather than silent.
    assert any("DIRECT_MATCH_REJECTED" in rejection.codes
               for rejection in graph.rejections)


# -- the quality path -----------------------------------------------------


def test_a_quality_request_returns_choice_required_and_never_selects():
    fixture = fx.make_stage6_fixture()
    outcome = quality_request(fixture)

    assert outcome.status is ObjectiveStatus.CHOICE_REQUIRED
    assert outcome.choice_required
    assert outcome.resolution is None          # nothing was auto-selected
    assert outcome.report.ranking_complete is False
    assert outcome.report.nondominance_claimed is False
    assert {item.capability_id for item in outcome.report.admissible} == {
        "example-flow-direct", "example-flow-model"}


def test_choice_required_holds_even_when_evidence_separates_cleanly():
    fixture = fx.make_stage6_fixture()
    outcome = quality_request(fixture)
    verdict = outcome.report.comparability

    # The evidence here is comparable and the intervals do not overlap, so a
    # ranking would be tempting.  The MVP still refuses to make the call.
    assert verdict.comparable
    assert verdict.intervals_overlap is False
    assert verdict.separation_established
    assert outcome.status is ObjectiveStatus.CHOICE_REQUIRED


def test_alternatives_carry_real_costs_from_constrained_re_solves():
    fixture = fx.make_stage6_fixture()
    report = quality_request(fixture).report
    by_id = {item.capability_id: item for item in report.alternatives}
    assert by_id["example-flow-model"].cost_units == fx.MINIMUM_COST_TOTAL
    assert by_id["example-flow-direct"].cost_units == fx.DIRECT_PATH_TOTAL
    # Each alternative is a whole plan, not a local swap.
    assert by_id["example-flow-model"].plan_id != by_id["example-flow-direct"].plan_id


def test_alternatives_force_the_exact_contested_satisfaction_arc():
    fixture = fx.make_stage6_fixture()
    baseline = _resolve(fixture)
    calls = []

    def resolve(constraints):
        outcome = _resolve(fixture, constraints)
        calls.append((constraints, outcome))
        return outcome

    report = build_choice_report(
        baseline, resolve, concept_id=fx.FLOW_CONCEPT,
        requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot)
    contested_use_id = next(
        node.use_id for node in baseline.hypergraph.use_nodes
        if node.requirement_id == fixture.flow_use.requirement.requirement_id
        and node.port_id == fixture.flow_use.port_id)

    assert len(calls) == len(report.alternatives) == 2
    for alternative, (constraints, outcome) in zip(report.alternatives, calls):
        assert constraints.include == ()
        assert constraints.required_satisfactions == (
            alternative.satisfaction,)
        assert alternative.satisfaction.use_id == contested_use_id
        assert outcome.selection.plan is not None
        binding = next(
            item for item in outcome.selection.plan.satisfactions
            if item.use_id == contested_use_id)
        assert any(
            output.producer_kind is alternative.satisfaction.producer_kind
            and output.producer_id == alternative.satisfaction.producer_id
            and output.output_port_id
            == alternative.satisfaction.output_port_id
            for output in binding.outputs)


def test_recorded_choice_reuses_the_presented_exact_arc_constraint():
    fixture = fx.make_stage6_fixture()
    report = quality_request(fixture).report
    alternative = next(
        item for item in report.alternatives
        if item.capability_id == "example-flow-direct")
    choice = ChoiceRecord(
        report.report_id, alternative.producer, "exact-arc-choice-v1",
        "exercise the exact contested satisfaction")
    calls = []

    def resolve(constraints):
        calls.append(constraints)
        return _resolve(fixture, constraints)

    outcome = resolve_with_objective(
        resolve,
        ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY, choice=choice),
        concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot)

    assert outcome.status is ObjectiveStatus.RESOLVED
    assert calls[-1].include == ()
    assert calls[-1].required_satisfactions == (alternative.satisfaction,)


def test_different_references_make_alternatives_incomparable():
    fixture = fx.make_stage6_fixture(
        model_reference_manifest_id=fx.OTHER_REFERENCE_MANIFEST_ID)
    report = quality_request(fixture).report
    assert not report.comparability.comparable
    assert not report.comparability.separation_established
    # The alternatives are still offered; only the comparison is refused.
    assert len(report.admissible) == 2


def test_evidence_comes_from_the_frozen_snapshot_not_the_caller():
    """An unrelated snapshot must yield no usable evidence.

    The audit found the report trusting a caller-supplied profile dictionary
    while ignoring the snapshot it was handed, so an empty unrelated snapshot
    still produced two admissible, comparable alternatives. Readings are now
    resolved by deriving each producer's evidence subject and matching it
    against the frozen snapshot.
    """
    from contracts import EvidenceProfile, EvidenceSnapshot, EvidenceSubject
    from objectives import build_choice_report

    fixture = fx.make_stage6_fixture()
    baseline = _resolve(fixture)

    unrelated = EvidenceSnapshot(
        "2026-08-15T00:00:00Z", fx.evaluator(),
        (EvidenceProfile(
            "ExampleEvidence-v1",
            EvidenceSubject("nobody", "0.0", "unrelated", "result"), ()),))
    with pytest.raises(ValueError, match="evidence snapshot differs"):
        build_choice_report(
            baseline, lambda constraints: _resolve(fixture, constraints),
            concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
            metric_definition_id=fx.METRIC_ID,
            evidence_snapshot=unrelated)


def test_report_uses_the_exact_profile_id_bound_to_each_producer():
    """A second profile for the same subject cannot replace the bound one."""
    from dataclasses import replace

    from contracts import (
        EstimateUncertainty,
        EvidenceClaim,
        EvidenceProfile,
        EvidenceSnapshot,
    )

    fixture = fx.make_stage6_fixture()
    direct_profile = next(
        profile for profile in fixture.evidence_snapshot.profiles
        if profile.claims[0].value == "0.9")
    direct_claim = direct_profile.claims[0]
    decoy_claim = EvidenceClaim.known(
        direct_claim.metric_definition_id, "0.3", direct_claim.unit,
        bound_kind=direct_claim.bound_kind,
        reference_manifest_id=direct_claim.reference_manifest_id,
        protocol_id=direct_claim.protocol_id,
        evaluator_id=direct_claim.evaluator_id,
        applicability=direct_claim.applicability,
        estimate_uncertainty=EstimateUncertainty.known(
            "0.2", "0.4", direct_claim.unit, "0.95",
            "example.block-bootstrap.v1"),
    )
    decoy = EvidenceProfile(
        direct_profile.schema_version, direct_profile.subject, (decoy_claim,),
        direct_profile.factual_metadata_ids)
    # The old subject-only lookup returned the first sorted profile. Make that
    # explicitly be the decoy so this is a regression, not a coincidental pass.
    assert decoy.profile_id < direct_profile.profile_id
    snapshot = EvidenceSnapshot(
        fixture.evidence_snapshot.captured_at,
        fixture.evidence_snapshot.evaluator,
        fixture.evidence_snapshot.profiles + (decoy,))
    fixture = replace(fixture, evidence_snapshot=snapshot)
    baseline = _resolve(fixture)

    report = build_choice_report(
        baseline, lambda constraints: _resolve(fixture, constraints),
        concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot)
    direct = next(
        item for item in report.alternatives
        if item.capability_id == "example-flow-direct")

    assert direct.reading is not None
    assert direct.reading.value == direct_claim.value
    assert direct.reading.value != decoy_claim.value


def test_alternatives_cannot_mix_different_frozen_resolution_universes():
    from objectives import build_choice_report

    fixture = fx.make_stage6_fixture()
    changed = fx.make_stage6_fixture(direct_interval=("0.10", "0.20"))
    baseline = _resolve(fixture)

    with pytest.raises(ValueError, match="another frozen planning universe"):
        build_choice_report(
            baseline, lambda constraints: _resolve(changed, constraints),
            concept_id=fx.FLOW_CONCEPT,
            requirement_use=fixture.flow_use,
            metric_definition_id=fx.METRIC_ID,
            evidence_snapshot=fixture.evidence_snapshot)


def test_a_recorded_choice_drives_a_constrained_re_solve():
    fixture = fx.make_stage6_fixture()
    report = quality_request(fixture).report
    chosen = apply_choice(fixture, report, "example-flow-direct")

    assert chosen.status is ObjectiveStatus.RESOLVED
    assert chosen.resolution.status is ResolutionStatus.READY
    assert "example-flow-direct" in _selected(chosen.resolution)
    # The human overrode minimum cost, and the system honoured it.
    assert chosen.resolution.selection.objective_cost_units == fx.DIRECT_PATH_TOTAL


def test_final_choice_re_solve_cannot_switch_frozen_resolution_universe():
    fixture = fx.make_stage6_fixture()
    changed = fx.make_stage6_fixture(direct_interval=("0.10", "0.20"))
    report = quality_request(fixture).report
    alternative = next(
        item for item in report.alternatives
        if item.capability_id == "example-flow-direct")
    choice = ChoiceRecord(
        report.report_id, alternative.producer, "context-switch-v1",
        "attempt to change deployment/evidence context after presentation")
    calls = 0

    def resolve(constraints):
        nonlocal calls
        calls += 1
        # Baseline and alternative enumeration use the presented universe;
        # only the final choice re-solve is switched.
        if calls == 4:
            return _resolve(changed, constraints)
        return _resolve(fixture, constraints)

    with pytest.raises(ValueError, match="final objective re-solve"):
        resolve_with_objective(
            resolve,
            ObjectiveRequest(
                SelectionObjective.EMPIRICAL_QUALITY, choice=choice),
            concept_id=fx.FLOW_CONCEPT,
            requirement_use=fixture.flow_use,
            metric_definition_id=fx.METRIC_ID,
            evidence_snapshot=fixture.evidence_snapshot)


def test_a_choice_changes_the_bound_plan_identity():
    fixture = fx.make_stage6_fixture()
    report = quality_request(fixture).report
    chosen = apply_choice(fixture, report, "example-flow-direct")

    with_decision = build_demo_plan(
        fixture, decision_ref=chosen.snapshot_ref, outcome=chosen.resolution)
    without_decision = build_demo_plan(fixture)
    assert (with_decision.bound_plan.bound_plan_id
            != without_decision.bound_plan.bound_plan_id)
    assert chosen.snapshot_ref.snapshot_id == chosen.decision_id


def test_a_stale_choice_is_refused_rather_than_applied():
    fixture = fx.make_stage6_fixture()
    report = quality_request(fixture).report
    alternative = report.admissible[0]
    stale = ChoiceRecord("f" * 64, alternative.producer, "v1", "stale choice")

    def resolve(constraints):
        return _resolve(fixture, constraints)

    with pytest.raises(StaleChoiceError):
        resolve_with_objective(
            resolve,
            ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY, choice=stale),
            concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
            metric_definition_id=fx.METRIC_ID,
            evidence_snapshot=fixture.evidence_snapshot,
            )


def test_a_minimum_cost_request_resolves_without_a_decision():
    fixture = fx.make_stage6_fixture()

    def resolve(constraints):
        return _resolve(fixture, constraints)

    outcome = resolve_with_objective(
        resolve, ObjectiveRequest(SelectionObjective.MINIMUM_COST),
        concept_id=fx.FLOW_CONCEPT, requirement_use=fixture.flow_use,
        metric_definition_id=fx.METRIC_ID,
        evidence_snapshot=fixture.evidence_snapshot,
        )
    assert outcome.status is ObjectiveStatus.RESOLVED
    assert outcome.decision_id is None and outcome.snapshot_ref is None


# -- execution ------------------------------------------------------------


def test_the_selected_derivation_executes_and_commits():
    with tempfile.TemporaryDirectory(prefix="stage6-demo-") as root:
        result = run_demo(Path(root))

    minimum = result["minimum_cost"]
    assert minimum["run_state"] == "SUCCEEDED"
    assert minimum["result_matches_expected"] is True
    assert minimum["task_count"] == 6      # four inputs plus model plus root
    assert minimum["attempt_count"] == 6
    assert minimum["cost_units"] == fx.MINIMUM_COST_TOTAL

    gating = result["evidence_gating"]
    assert gating["model_excluded_where_evidence_does_not_apply"] is True

    quality = result["quality_request"]
    assert quality["status"] == "CHOICE_REQUIRED"
    assert quality["auto_selected"] is False

    recorded = result["recorded_choice"]
    assert recorded["decision_changes_plan_identity"] is True


def test_evidence_bound_proofs_now_replay_in_the_compiler():
    """Stage 6 closed a gap: the compiler used to refuse these outright."""
    fixture = fx.make_stage6_fixture()
    demo = build_demo_plan(fixture)
    assert demo.compilation.record.status.value == "COMPILED"

    from composition import compile_bound_plan
    # Without the frozen evidence it must still fail closed rather than
    # replaying a weaker claim than the one that was selected.
    with pytest.raises(ValueError, match="requires the frozen evidence"):
        compile_bound_plan(
            demo.bound_plan, demo.selected_invocations,
            deployment_plan=demo.deployment_plan, name="no-evidence",
            deployment_snapshot=fixture.deployment_snapshot,
            execution_profiles=fixture.catalog.execution_profiles,
            root_uses=fixture.root_uses, artifact_leaves=())
