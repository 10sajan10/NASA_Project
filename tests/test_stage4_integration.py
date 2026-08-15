"""Stage-4 end-to-end: transformation closure through validated commit.

The central claim under test is that an explicit transformation competes with
direct data as an ordinary costed producer, and that truncating transformation
discovery is never allowed to masquerade as a globally optimal answer.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from resolution import ResolutionStatus, WorkflowResolver
from stage4.demo import build_demo_plan, run_demo
from stage4.fixtures import (
    EXPECTED_KILOMETRES,
    SOURCE_METRES,
    make_unit_bridge_fixture,
)
from transformations import (
    TransformationLimitCode,
    TransformationSearchLimits,
    expand_transform_catalog,
)


def _resolver(expansion, fixture, **kwargs):
    return WorkflowResolver(
        expansion.augmented_catalog,
        fixture.deployment_snapshot,
        upstream_discovery_complete=expansion.complete,
        upstream_limit_codes=tuple(sorted({
            value.code.value for value in expansion.limit_reasons})),
        **kwargs,
    )


def test_explicit_transformation_beats_a_more_expensive_direct_source():
    demo = build_demo_plan()

    assert demo.resolution.status is ResolutionStatus.READY
    assert demo.resolution.eligible_for_binding
    selected = sorted(
        value.capability_id for value in demo.selected_invocations)
    assert selected == list(demo.fixture.expected_capability_ids)
    # Cost 4 + 1 through the transformation, versus 9 for direct kilometres.
    assert (demo.resolution.selection.objective_cost_units
            == demo.fixture.expected_cost_units == 5)
    # The direct producer really was a candidate; it lost on cost rather than
    # being absent from the graph.
    discovered = {
        value.invocation.capability_id
        for value in demo.resolution.hypergraph.invocation_nodes}
    assert "example-length-kilometres-direct" in discovered


def test_transformation_is_a_visible_plan_node_not_a_hidden_adapter_step():
    demo = build_demo_plan()
    graph = demo.compilation.graph
    assert graph is not None

    transform_invocations = [
        value for value in demo.selected_invocations
        if value.capability_id.startswith("transform:")]
    assert len(transform_invocations) == 1
    # It is a real compiled task with its own attempt, cost, and provenance,
    # not an implicit conversion inside the consumer.
    assert len(graph.tasks) == 2
    bound_ids = {
        value.invocation_id
        for value in demo.bound_plan.invocation_bindings}
    assert transform_invocations[0].invocation_key in bound_ids
    # Transformation closure identity is recorded in the bound plan so the
    # derivation can be replayed against the same descriptor-state universe.
    assert any(value.name == "transformation_expansion"
               and value.snapshot_id == demo.expansion.expansion_id
               for value in demo.bound_plan.scientific_snapshot_refs)


def test_requirement_rejecting_derived_origin_excludes_the_transform_path():
    import dataclasses

    from contracts import OriginClass, RequirementUse

    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    root = fixture.root_uses[0]
    synthetic_only = RequirementUse(
        root.use_id, root.port_id,
        dataclasses.replace(
            root.requirement, allowed_origins=(OriginClass.SYNTHETIC,)),
    )

    outcome = _resolver(expansion, fixture).resolve((synthetic_only,))

    # Origin is a consumer constraint, so refusing derived products must fall
    # back to the expensive direct source rather than silently transforming.
    assert outcome.status is ResolutionStatus.READY
    assert outcome.selection.objective_cost_units == 9
    selected = {
        value.invocation.capability_id
        for value in outcome.hypergraph.invocation_nodes
        if value.invocation_id in set(
            outcome.selection.plan.selected_invocation_ids)}
    assert selected == {"example-length-kilometres-direct"}


@pytest.mark.parametrize(
    "limits,expected_code",
    [
        (TransformationSearchLimits(max_depth=0),
         TransformationLimitCode.MAX_DEPTH),
        (TransformationSearchLimits(max_transformations=0),
         TransformationLimitCode.MAX_TRANSFORMATIONS),
    ],
)
def test_truncated_closure_cannot_yield_a_global_optimality_claim(
        limits, expected_code):
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations, limits=limits)
    assert not expansion.complete

    outcome = _resolver(expansion, fixture).resolve(fixture.root_uses)

    # A selection optimal over a catalog that is missing candidates is not a
    # global optimum.  Stage 3's own expansion was complete here, so only the
    # upstream transformation truncation can be responsible.
    assert outcome.hypergraph.discovery_complete
    assert not outcome.selection.discovery_complete
    assert not outcome.selection.globally_optimal_over_discovery_space
    assert outcome.status is ResolutionStatus.FEASIBLE_NOT_PROVEN_OPTIMAL
    assert not outcome.eligible_for_binding
    assert expected_code.value in outcome.upstream_limit_codes
    # The plan itself is structurally sound; only the optimality claim fails.
    assert outcome.validation is not None and outcome.validation.valid


def test_truncated_closure_is_bindable_only_without_a_proof_requirement():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations,
        limits=TransformationSearchLimits(max_depth=0))

    outcome = _resolver(expansion, fixture).resolve(
        fixture.root_uses, require_proven_optimal=False)

    # An explicit caller may accept a best-effort plan, but the recorded
    # status still refuses to call it globally optimal.
    assert outcome.eligible_for_binding
    assert outcome.status is ResolutionStatus.FEASIBLE_NOT_PROVEN_OPTIMAL
    assert not outcome.selection.globally_optimal_over_discovery_space


def test_upstream_completeness_flags_must_be_internally_consistent():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)

    with pytest.raises(ValueError, match="must name its limit codes"):
        WorkflowResolver(
            expansion.augmented_catalog, fixture.deployment_snapshot,
            upstream_discovery_complete=False)
    with pytest.raises(ValueError, match="cannot report limit codes"):
        WorkflowResolver(
            expansion.augmented_catalog, fixture.deployment_snapshot,
            upstream_discovery_complete=True,
            upstream_limit_codes=("MAX_DEPTH",))


def test_outcome_report_separates_effective_and_local_completeness():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations,
        limits=TransformationSearchLimits(max_depth=0))

    payload = _resolver(expansion, fixture).resolve(
        fixture.root_uses).to_dict()["discovery"]

    # A reader must be able to see both that the answer is not complete and
    # which layer truncated, without re-deriving it.
    assert payload["complete"] is False
    assert payload["graph_expansion_complete"] is True
    assert payload["upstream_limit_codes"] == ["MAX_DEPTH"]


def test_full_vertical_slice_executes_and_commits_the_converted_value():
    with tempfile.TemporaryDirectory(prefix="nasa-stage4-test.") as root:
        result = run_demo(Path(root))

    assert result["run_state"] == "SUCCEEDED"
    assert result["resolution_status"] == "READY"
    assert result["validation_passed"] is True
    assert result["globally_optimal"] is True
    assert result["transformation_closure_complete"] is True
    assert result["selected_transformations"] == [
        "transform:example-metres-to-kilometres"]
    assert result["task_count"] == 2
    assert result["attempt_count"] == 2
    # The committed value is the converted quantity, proving the transform
    # actually ran rather than the source passing through unchanged.
    assert result["result"] == pytest.approx(EXPECTED_KILOMETRES)
    assert result["result"] == pytest.approx(SOURCE_METRES / 1000.0)
