"""Stage-4 end-to-end: transformation closure through validated commit.

The central claim under test is that an explicit transformation competes with
direct data as an ordinary costed producer, and that truncating transformation
discovery is never allowed to masquerade as a globally optimal answer.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from capabilities import CapabilityCatalog, DiscoveryLayerCertificate
from resolution import (
    DiscoveryCertificate,
    DiscoveryLayerScope,
    DiscoveryUniverseContract,
    MilpSelectionProblem,
    ResolutionStatus,
    WorkflowResolver,
    project_oracle_problem,
    solve_milp,
)
from stage4.demo import build_demo_plan, run_demo
from stage4.fixtures import (
    EXPECTED_KILOMETRES,
    SOURCE_METRES,
    make_unit_bridge_fixture,
)
from transformations import (
    TransformationCatalog,
    TransformationDiscoveryReplay,
    TransformationLimitCode,
    TransformationSearchLimits,
    expand_transform_catalog,
)


def _resolver(expansion, fixture, **kwargs):
    universe = kwargs.pop("discovery_universe", None)
    if universe is None:
        universe = DiscoveryUniverseContract.declare(
            fixture.base_catalog.catalog_id,
            (DiscoveryLayerScope.bind(
                "TRANSFORMATION_EXPANSION",
                source_ids=(expansion.transformation_catalog_id,),
                limits=expansion.limits.to_dict()),),
        )
    return WorkflowResolver(
        expansion.augmented_catalog,
        fixture.deployment_snapshot,
        discovery_certificate=expansion.discovery_certificate(),
        discovery_universe=universe,
        discovery_replays=(TransformationDiscoveryReplay.bind(
            fixture.base_catalog,
            TransformationCatalog.freeze(fixture.transformations),
            limits=expansion.limits),),
        **kwargs,
    )


def _two_transformation_layers():
    fixture = make_unit_bridge_fixture()
    catalog = TransformationCatalog.freeze(fixture.transformations)
    first = expand_transform_catalog(fixture.base_catalog, catalog)
    second = expand_transform_catalog(first.augmented_catalog, catalog)
    scope = DiscoveryLayerScope.bind(
        "TRANSFORMATION_EXPANSION",
        source_ids=(catalog.catalog_id,),
        limits=TransformationSearchLimits().to_dict(),
    )
    universe = DiscoveryUniverseContract.declare(
        first.augmented_catalog.catalog_id, (scope, scope))
    replays = (
        TransformationDiscoveryReplay.bind(fixture.base_catalog, catalog),
        TransformationDiscoveryReplay.bind(first.augmented_catalog, catalog),
    )
    return fixture, catalog, first, second, universe, replays


def test_two_same_kind_layers_replay_by_exact_expansion_identity():
    fixture, _catalog, first, second, universe, replays = (
        _two_transformation_layers())
    assert first.expansion_id != second.expansion_id
    assert len(second.discovery_certificate().layers) == 2

    outcome = WorkflowResolver(
        second.augmented_catalog,
        fixture.deployment_snapshot,
        discovery_certificate=second.discovery_certificate(),
        discovery_universe=universe,
        # Deliberately reverse them: mapping is by exact replay output, not
        # tuple position or the non-unique TRANSFORMATION_EXPANSION label.
        discovery_replays=tuple(reversed(replays)),
    ).resolve(fixture.root_uses)

    assert outcome.status is ResolutionStatus.READY
    assert outcome.eligible_for_binding


def test_one_replay_cannot_cover_two_same_kind_layers():
    fixture, _catalog, _first, second, universe, replays = (
        _two_transformation_layers())
    with pytest.raises(
            ValueError, match="every discovered catalog layer requires"):
        WorkflowResolver(
            second.augmented_catalog,
            fixture.deployment_snapshot,
            discovery_certificate=second.discovery_certificate(),
            discovery_universe=universe,
            discovery_replays=(replays[1],),
        )


def test_replay_chain_rejects_an_unaccounted_final_capability():
    from stage3.fixtures import make_composition_fixture

    fixture, _catalog, _first, second, universe, replays = (
        _two_transformation_layers())
    donor = make_composition_fixture().catalog
    extra = donor.capabilities[0]
    extra_profile = donor.profile(extra.execution_profile_id)
    profiles = {
        item.profile_id: item
        for item in second.augmented_catalog.execution_profiles
    }
    profiles[extra_profile.profile_id] = extra_profile
    forged = CapabilityCatalog.freeze(
        (*second.augmented_catalog.capabilities, extra),
        profiles.values(),
        discovery_base_catalog_id=(
            second.augmented_catalog.discovery_provenance.base_catalog_id),
        discovery_layers=(
            second.augmented_catalog.discovery_provenance.layers),
    )

    with pytest.raises(ValueError, match="exact chain to the final catalog"):
        WorkflowResolver(
            forged,
            fixture.deployment_snapshot,
            discovery_certificate=DiscoveryCertificate.for_catalog(forged),
            discovery_universe=universe,
            discovery_replays=replays,
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


def test_discovery_certificate_round_trip_is_strict_and_content_addressed():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    certificate = expansion.discovery_certificate()

    assert DiscoveryCertificate.from_dict(certificate.to_dict()) == certificate
    assert certificate.subject_catalog_id == expansion.augmented_catalog.catalog_id
    assert certificate.base_catalog_id == fixture.base_catalog.catalog_id
    assert certificate.expansion_ids == (expansion.expansion_id,)
    assert certificate.layers[0].source_ids == (
        expansion.transformation_catalog_id,)
    assert certificate.layers[0].limits == tuple(sorted(
        certificate.layers[0].limits))

    tampered = certificate.to_dict()
    tampered["subject_catalog_id"] = "0" * 64
    with pytest.raises(ValueError, match="identity does not verify"):
        DiscoveryCertificate.from_dict(tampered)

    tampered_limit = certificate.to_dict()
    tampered_limit["layers"][0]["limits"][0]["value"] += 1
    with pytest.raises(ValueError, match="identity does not verify"):
        DiscoveryCertificate.from_dict(tampered_limit)

    unexpected = certificate.to_dict()
    unexpected["unrecognized"] = True
    with pytest.raises(ValueError, match="unexpected"):
        DiscoveryCertificate.from_dict(unexpected)


def test_resolver_refuses_a_certificate_for_another_catalog():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    wrong = DiscoveryCertificate.for_base_catalog(fixture.base_catalog)

    with pytest.raises(ValueError, match="another capability catalog"):
        WorkflowResolver(
            expansion.augmented_catalog,
            fixture.deployment_snapshot,
            discovery_certificate=wrong,
            discovery_universe=DiscoveryUniverseContract.declare(
                fixture.base_catalog.catalog_id),
        )


def test_bare_truncated_augmented_catalog_is_refused_before_resolution():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog,
        fixture.transformations,
        limits=TransformationSearchLimits(max_depth=0),
    )
    assert not expansion.complete
    # With depth zero no transformation capability was added, so the typed
    # contents are identical. The catalogs are nevertheless distinct because
    # the truncated discovery record is part of catalog identity.
    assert expansion.augmented_catalog.content_id \
        == fixture.base_catalog.content_id
    assert expansion.augmented_catalog.catalog_id \
        != fixture.base_catalog.catalog_id
    assert not expansion.augmented_catalog.discovery_provenance.complete
    assert CapabilityCatalog.from_dict(
        expansion.augmented_catalog.to_dict()) == expansion.augmented_catalog
    tampered_catalog = expansion.augmented_catalog.to_dict()
    tampered_catalog["discovery_provenance"]["layers"][0][
        "limit_reasons"][0]["limit"] += 1
    with pytest.raises(ValueError, match="catalog identity does not verify"):
        CapabilityCatalog.from_dict(tampered_catalog)
    certificate = expansion.discovery_certificate()
    tampered_reason = certificate.to_dict()
    tampered_reason["layers"][0]["limit_reasons"][0]["limit"] += 1
    with pytest.raises(ValueError, match="identity does not verify"):
        DiscoveryCertificate.from_dict(tampered_reason)

    # Catalog shape cannot reveal that the preceding search truncated. There
    # is therefore no default-true path: both the exact certificate and the
    # independently declared universe are mandatory.
    with pytest.raises(TypeError, match="discovery_certificate"):
        WorkflowResolver(
            expansion.augmented_catalog, fixture.deployment_snapshot)
    with pytest.raises(TypeError, match="discovery_universe"):
        WorkflowResolver(
            expansion.augmented_catalog,
            fixture.deployment_snapshot,
            discovery_certificate=expansion.discovery_certificate(),
        )

    # The generated object itself cannot be directly relabelled because its
    # discovery provenance is in catalog identity. Copying its contents into a
    # newly authored catalog is the stronger attack covered by the next test.
    with pytest.raises(ValueError, match="cannot be relabelled"):
        DiscoveryCertificate.for_base_catalog(expansion.augmented_catalog)

    # Nor can a caller handcraft the previous empty-layer assertion: resolver
    # reconstructs the only acceptable certificate from catalog provenance.
    self_attested = DiscoveryCertificate.bind(
        expansion.augmented_catalog.catalog_id,
        expansion.augmented_catalog.catalog_id,
    )
    with pytest.raises(ValueError, match="does not match catalog provenance"):
        WorkflowResolver(
            expansion.augmented_catalog,
            fixture.deployment_snapshot,
            discovery_certificate=self_attested,
            discovery_universe=DiscoveryUniverseContract.declare(
                expansion.augmented_catalog.catalog_id),
        )


def test_predeclared_universe_rejects_copied_truncated_catalog_contents():
    """Copying generated contents cannot erase a required discovery layer."""
    fixture = make_unit_bridge_fixture()
    limits = TransformationSearchLimits(max_depth=0)
    transform_catalog = TransformationCatalog.freeze(fixture.transformations)
    universe = DiscoveryUniverseContract.declare(
        fixture.base_catalog.catalog_id,
        (DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=(transform_catalog.catalog_id,),
            limits=limits.to_dict()),),
    )
    expansion = expand_transform_catalog(
        fixture.base_catalog, transform_catalog, limits=limits)
    assert not expansion.complete

    # This is the exact earlier bypass: public freeze treats copied typed
    # contents as authored. At depth zero those contents equal the real base,
    # so even the declared base ID still matches. Only the independently
    # supplied required-layer scope exposes the missing transformation search.
    copied = CapabilityCatalog.freeze(
        expansion.augmented_catalog.capabilities,
        expansion.augmented_catalog.execution_profiles,
    )
    assert copied.catalog_id == fixture.base_catalog.catalog_id
    copied_certificate = DiscoveryCertificate.for_base_catalog(copied)

    with pytest.raises(
            ValueError, match="does not cover the declared universe"):
        WorkflowResolver(
            copied,
            fixture.deployment_snapshot,
            discovery_certificate=copied_certificate,
            discovery_universe=universe,
        )


def test_forged_complete_layer_is_rejected_by_deterministic_replay():
    """Catalog hashes cannot self-attest that a closure really completed."""
    fixture = make_unit_bridge_fixture()
    limits = TransformationSearchLimits(max_depth=0)
    transform_catalog = TransformationCatalog.freeze(fixture.transformations)
    real = expand_transform_catalog(
        fixture.base_catalog, transform_catalog, limits=limits)
    assert not real.complete

    forged_layer = DiscoveryLayerCertificate.bind(
        "TRANSFORMATION_EXPANSION",
        real.expansion_id,
        source_ids=(transform_catalog.catalog_id,),
        limits=limits.to_dict(),
        limit_reasons=(),
        complete=True,
    )
    forged_catalog = CapabilityCatalog.freeze(
        real.augmented_catalog.capabilities,
        real.augmented_catalog.execution_profiles,
        discovery_base_catalog_id=fixture.base_catalog.catalog_id,
        discovery_layers=(forged_layer,),
    )
    universe = DiscoveryUniverseContract.declare(
        fixture.base_catalog.catalog_id,
        (DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=(transform_catalog.catalog_id,),
            limits=limits.to_dict()),),
    )

    with pytest.raises(ValueError, match="independent replay"):
        WorkflowResolver(
            forged_catalog,
            fixture.deployment_snapshot,
            discovery_certificate=DiscoveryCertificate.for_catalog(
                forged_catalog),
            discovery_universe=universe,
            discovery_replays=(TransformationDiscoveryReplay.bind(
                fixture.base_catalog, transform_catalog, limits=limits),),
        )


def test_discovery_universe_round_trip_and_identity_are_strict():
    fixture = make_unit_bridge_fixture()
    transform_catalog = TransformationCatalog.freeze(fixture.transformations)
    universe = DiscoveryUniverseContract.declare(
        fixture.base_catalog.catalog_id,
        (DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=(transform_catalog.catalog_id,),
            limits=TransformationSearchLimits().to_dict()),),
    )

    assert DiscoveryUniverseContract.from_dict(
        universe.to_dict()) == universe
    tampered = universe.to_dict()
    tampered["required_layers"][0]["limits"][0]["value"] += 1
    with pytest.raises(ValueError, match="identity does not verify"):
        DiscoveryUniverseContract.from_dict(tampered)


def test_discovery_universe_requires_exact_sources_limits_and_layer_set():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    certificate = expansion.discovery_certificate()

    for scope in (
        DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=("different-transform-source",),
            limits=expansion.limits.to_dict()),
        DiscoveryLayerScope.bind(
            "TRANSFORMATION_EXPANSION",
            source_ids=(expansion.transformation_catalog_id,),
            limits=TransformationSearchLimits(max_depth=1).to_dict()),
    ):
        universe = DiscoveryUniverseContract.declare(
            fixture.base_catalog.catalog_id, (scope,))
        with pytest.raises(
                ValueError, match="does not cover the declared universe"):
            universe.verify_coverage(certificate)

    empty = DiscoveryUniverseContract.declare(
        fixture.base_catalog.catalog_id)
    with pytest.raises(
            ValueError, match="does not cover the declared universe"):
        empty.verify_coverage(certificate)


def test_discovery_certificate_identity_is_load_bearing_in_selector_and_plan():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    outcome = _resolver(expansion, fixture).resolve(fixture.root_uses)

    first_graph = project_oracle_problem(
        outcome.hypergraph, discovery_certificate_id="1" * 64)
    second_graph = project_oracle_problem(
        outcome.hypergraph, discovery_certificate_id="2" * 64)
    first_request = MilpSelectionProblem.bind(first_graph)
    second_request = MilpSelectionProblem.bind(second_graph)
    first = solve_milp(first_request)
    second = solve_milp(second_request)

    assert first_request.selection_problem_id \
        != second_request.selection_problem_id
    assert first.plan is not None and second.plan is not None
    assert first.plan.plan_id != second.plan.plan_id
    assert first.plan.selected_invocation_ids \
        == second.plan.selected_invocation_ids
    assert dict((item.name, item.snapshot_id)
                for item in outcome.selector_problem.snapshot_refs)[
                    "discovery_certificate"] \
        == outcome.discovery_certificate.certificate_id
    assert dict((item.name, item.snapshot_id)
                for item in outcome.selector_problem.snapshot_refs)[
                    "discovery_universe"] \
        == outcome.discovery_universe.universe_id


def test_discovery_universe_identity_is_load_bearing_in_selector_and_plan():
    fixture = make_unit_bridge_fixture()
    expansion = expand_transform_catalog(
        fixture.base_catalog, fixture.transformations)
    first_graph = project_oracle_problem(
        _resolver(expansion, fixture).resolve(fixture.root_uses).hypergraph,
        discovery_certificate_id=expansion.discovery_certificate().certificate_id,
        discovery_universe_id="3" * 64,
    )
    second_graph = project_oracle_problem(
        _resolver(expansion, fixture).resolve(fixture.root_uses).hypergraph,
        discovery_certificate_id=expansion.discovery_certificate().certificate_id,
        discovery_universe_id="4" * 64,
    )
    first = solve_milp(MilpSelectionProblem.bind(first_graph))
    second = solve_milp(MilpSelectionProblem.bind(second_graph))

    assert first_graph.problem_id != second_graph.problem_id
    assert first.plan is not None and second.plan is not None
    assert first.plan.plan_id != second.plan.plan_id
    assert first.plan.selected_invocation_ids \
        == second.plan.selected_invocation_ids


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
    assert payload["claim_scope"] \
        == "DECLARED_CERTIFICATE_COVERED_UNIVERSE"
    assert payload["graph_expansion_complete"] is True
    assert payload["upstream_limit_codes"] == ["MAX_DEPTH"]
    certificate = payload["certificate"]
    assert certificate["subject_catalog_id"] == expansion.augmented_catalog.catalog_id
    assert certificate["base_catalog_id"] == fixture.base_catalog.catalog_id
    assert certificate["layers"][0]["expansion_id"] == expansion.expansion_id


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
