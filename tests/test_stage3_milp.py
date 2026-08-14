from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from composition.oracle import (
    ArtifactLeafNode,
    CardinalityRange,
    DistinctBy,
    InvocationNode,
    OracleProblem,
    OracleStatus,
    RequirementUseNode,
    SatisfactionArc,
    exhaustive_enumerate,
)
from plans import PlanSnapshotRef
from contracts import (
    CheckStatus,
    CompatibilityCheck,
    CompatibilityProof,
    MatchCode,
)
from plans import CompatibilityProofRecord, ProducerKind, SatisfactionKind
from resolution.milp import (
    DeploymentOption,
    MilpSelectionProblem,
    MilpSolveOptions,
    MilpStatus,
    ProducerSelectionRef,
    SelectionConstraints,
    solve_milp,
)
from resolution.hypergraph import build_feasible_hypergraph
from stage3.fixtures import make_composition_fixture


def _proof(use_id: str, producer_id: str, port: str) -> CompatibilityProofRecord:
    proof = CompatibilityProof(
        requirement_id=f"requirement:{use_id}",
        descriptor_id=f"descriptor:{producer_id}:{port}",
        evidence_profile_id=None,
        evidence_snapshot_id=None,
        evidence_subject_id=None,
        checks=(CompatibilityCheck(
            dimension="synthetic",
            status=CheckStatus.PASS,
            code=MatchCode.MATCH,
            detail="closed Stage-3 selector fixture",
        ),),
    )
    return CompatibilityProofRecord.from_compatibility(proof)


def _arc(
        use_id: str, producer_id: str, *, port: str = "result",
        kind: ProducerKind = ProducerKind.INVOCATION,
) -> SatisfactionArc:
    return SatisfactionArc(
        use_id=use_id,
        producer_id=producer_id,
        producer_kind=kind,
        output_port_id=port,
        proof=_proof(use_id, producer_id, port),
    )


def _source(name: str, cost: int, *ports: str) -> InvocationNode:
    return InvocationNode(
        invocation_id=name,
        input_use_ids=(),
        output_port_ids=tuple(sorted(ports or ("result",))),
        cost_units=cost,
        zero_input_source=True,
    )


def _solve(graph: OracleProblem, **kwargs: object):
    return solve_milp(MilpSelectionProblem.bind(graph, **kwargs))


def test_exact_cost_and_tie_order_match_independent_oracle() -> None:
    root = RequirementUseNode("root", "same", "result")
    a = _source("a-source", 1)
    b = _source("b-source", 1)
    graph = OracleProblem.bind(
        "tie",
        uses=(root,),
        root_use_ids=(root.use_id,),
        invocations=(a, b),
        satisfaction_arcs=(
            _arc(root.use_id, a.invocation_id),
            _arc(root.use_id, b.invocation_id),
        ),
    )

    oracle = exhaustive_enumerate(graph)
    selected = _solve(graph)

    assert oracle.status is OracleStatus.OPTIMAL
    assert selected.status is MilpStatus.OPTIMAL
    assert selected.primary_cost_proven
    assert selected.tie_break_complete
    assert selected.plan.plan_id == oracle.optimal_plan.plan_id
    assert selected.plan.selected_invocation_ids == ("b-source",)
    assert selected.globally_optimal_over_discovery_space


def test_global_sharing_beats_local_private_choices() -> None:
    root_a = RequirementUseNode("root-a", "final-a", "result")
    root_b = RequirementUseNode("root-b", "final-b", "result")
    input_a = RequirementUseNode(
        "input-a", "shared-input", "input",
        owner_invocation_id="consumer-a")
    input_b = RequirementUseNode(
        "input-b", "shared-input", "input",
        owner_invocation_id="consumer-b")
    consumer_a = InvocationNode(
        "consumer-a", (input_a.use_id,), ("result",), 0)
    consumer_b = InvocationNode(
        "consumer-b", (input_b.use_id,), ("result",), 0)
    shared = _source("shared-source", 5)
    private_a = _source("private-a", 3)
    private_b = _source("private-b", 3)
    graph = OracleProblem.bind(
        "global-sharing-counterexample",
        uses=(root_a, root_b, input_a, input_b),
        root_use_ids=(root_a.use_id, root_b.use_id),
        invocations=(consumer_a, consumer_b, shared, private_a, private_b),
        satisfaction_arcs=(
            _arc(root_a.use_id, consumer_a.invocation_id),
            _arc(root_b.use_id, consumer_b.invocation_id),
            _arc(input_a.use_id, shared.invocation_id),
            _arc(input_b.use_id, shared.invocation_id),
            _arc(input_a.use_id, private_a.invocation_id),
            _arc(input_b.use_id, private_b.invocation_id),
        ),
    )

    result = _solve(graph)

    assert result.status is MilpStatus.OPTIMAL
    assert result.plan.total_cost_units == 5
    assert set(result.plan.selected_invocation_ids) == {
        "consumer-a", "consumer-b", "shared-source"}
    assert "private-a" not in result.plan.selected_invocation_ids
    assert "private-b" not in result.plan.selected_invocation_ids


def test_coproduct_cardinality_default_and_omit_match_oracle() -> None:
    ensemble = RequirementUseNode(
        "ensemble", "member", "members",
        cardinality=CardinalityRange(2, 2),
        distinct_by=DistinctBy.PRODUCER,
    )
    optional = RequirementUseNode(
        "optional", "annotation", "annotation",
        owner_invocation_id="consumer", optional=True,
        default_id="default:none", cardinality=CardinalityRange(0, 1),
    )
    root = RequirementUseNode("root", "final", "result")
    consumer = InvocationNode("consumer", (optional.use_id,), ("result",), 0)
    pair = _source("pair", 0, "member-a", "member-b")
    other = _source("other", 1)
    optional_source = _source("optional-source", 0)
    graph = OracleProblem.bind(
        "mixed-semantics",
        uses=(ensemble, optional, root),
        root_use_ids=(ensemble.use_id, root.use_id),
        invocations=(consumer, pair, other, optional_source),
        satisfaction_arcs=(
            _arc(root.use_id, consumer.invocation_id),
            _arc(optional.use_id, optional_source.invocation_id),
            _arc(ensemble.use_id, pair.invocation_id, port="member-a"),
            _arc(ensemble.use_id, pair.invocation_id, port="member-b"),
            _arc(ensemble.use_id, other.invocation_id),
        ),
    )

    oracle = exhaustive_enumerate(graph)
    selected = _solve(graph)

    assert selected.status is MilpStatus.OPTIMAL
    assert selected.plan.plan_id == oracle.optimal_plan.plan_id
    ensemble_binding = next(value for value in selected.plan.satisfactions
                            if value.use_id == "ensemble")
    assert len(ensemble_binding.outputs) == 2
    assert len({value.producer_id for value in ensemble_binding.outputs}) == 2
    optional_binding = next(value for value in selected.plan.satisfactions
                            if value.use_id == "optional")
    assert optional_binding.kind in {
        SatisfactionKind.DEFAULT, SatisfactionKind.OMIT,
        SatisfactionKind.PRODUCERS}


def test_coproduced_outputs_are_costed_once() -> None:
    left = RequirementUseNode("left", "value", "left")
    right = RequirementUseNode("right", "value", "right")
    pair = _source("pair", 3, "left", "right")
    left_only = _source("left-only", 2)
    right_only = _source("right-only", 2)
    graph = OracleProblem.bind(
        "coproduction",
        uses=(left, right),
        root_use_ids=(left.use_id, right.use_id),
        invocations=(pair, left_only, right_only),
        satisfaction_arcs=(
            _arc(left.use_id, pair.invocation_id, port="left"),
            _arc(right.use_id, pair.invocation_id, port="right"),
            _arc(left.use_id, left_only.invocation_id),
            _arc(right.use_id, right_only.invocation_id),
        ),
    )

    oracle = exhaustive_enumerate(graph)
    result = _solve(graph)

    assert result.plan.plan_id == oracle.optimal_plan.plan_id
    assert result.plan.selected_invocation_ids == ("pair",)
    assert result.plan.total_cost_units == 3


def test_include_exclude_and_budget_resolve_requested_alternative() -> None:
    root = RequirementUseNode("root", "value", "result")
    cheap = _source("cheap", 1)
    alternate = _source("alternate", 2)
    graph = OracleProblem.bind(
        "policy",
        uses=(root,), root_use_ids=(root.use_id,),
        invocations=(cheap, alternate),
        satisfaction_arcs=(
            _arc(root.use_id, cheap.invocation_id),
            _arc(root.use_id, alternate.invocation_id),
        ),
    )
    alternate_ref = ProducerSelectionRef(
        ProducerKind.INVOCATION, alternate.invocation_id)
    cheap_ref = ProducerSelectionRef(
        ProducerKind.INVOCATION, cheap.invocation_id)

    included = solve_milp(MilpSelectionProblem.bind(
        graph,
        constraints=SelectionConstraints.bind(include=(alternate_ref,)),
    ))
    excluded = solve_milp(MilpSelectionProblem.bind(
        graph,
        constraints=SelectionConstraints.bind(exclude=(cheap_ref,)),
    ))
    over_budget = solve_milp(MilpSelectionProblem.bind(
        graph,
        constraints=SelectionConstraints.bind(maximum_cost_units=0),
    ))

    assert included.plan.selected_invocation_ids == ("alternate",)
    assert excluded.plan.selected_invocation_ids == ("alternate",)
    assert over_budget.status is MilpStatus.UNSATISFIABLE
    assert "COST_BUDGET" in {value.code for value in over_budget.blockers}


def test_cycles_uncommitted_artifacts_and_missing_sites_are_hard_constraints() -> None:
    root = RequirementUseNode("root", "value", "result")
    a_input = RequirementUseNode(
        "a-input", "feedback", "input", owner_invocation_id="a")
    b_input = RequirementUseNode(
        "b-input", "feedback", "input", owner_invocation_id="b")
    a = InvocationNode("a", (a_input.use_id,), ("result",), 1)
    b = InvocationNode("b", (b_input.use_id,), ("result",), 1)
    uncommitted = ArtifactLeafNode(
        "uncommitted", ("artifact",), committed=False)
    graph = OracleProblem.bind(
        "hard-constraints",
        uses=(root, a_input, b_input),
        root_use_ids=(root.use_id,),
        invocations=(a, b),
        artifact_leaves=(uncommitted,),
        satisfaction_arcs=(
            _arc(root.use_id, a.invocation_id),
            _arc(root.use_id, uncommitted.leaf_id, port="artifact",
                 kind=ProducerKind.ARTIFACT_LEAF),
            _arc(a_input.use_id, b.invocation_id),
            _arc(b_input.use_id, a.invocation_id),
        ),
    )

    result = _solve(graph)

    assert result.status is MilpStatus.UNSATISFIABLE
    assert result.plan is None

    source = _source("source", 1)
    simple = OracleProblem.bind(
        "no-site", uses=(root,), root_use_ids=(root.use_id,),
        invocations=(source,), satisfaction_arcs=(
            _arc(root.use_id, source.invocation_id),))
    forced = ProducerSelectionRef(ProducerKind.INVOCATION, source.invocation_id)
    no_site = solve_milp(MilpSelectionProblem.bind(
        simple,
        deployment_options=(DeploymentOption(
            source.invocation_id, "site:no-access", False,
            ("ACCOUNT_DENIED",)),),
        constraints=SelectionConstraints.bind(include=(forced,)),
    ))
    assert no_site.status is MilpStatus.UNSATISFIABLE
    deployment_blocker = next(value for value in no_site.blockers
                              if value.code == "DEPLOYMENT_INFEASIBLE")
    assert deployment_blocker.details["site_blocker_codes"] == (
        "ACCOUNT_DENIED",)


def test_solver_optimality_and_discovery_completeness_are_independent() -> None:
    root = RequirementUseNode("root", "value", "result")
    source = _source("source", 1)
    graph = OracleProblem.bind(
        "bounded-discovery",
        uses=(root,), root_use_ids=(root.use_id,),
        invocations=(source,), satisfaction_arcs=(
            _arc(root.use_id, source.invocation_id),))

    result = _solve(
        graph, discovery_complete=False,
        discovery_limit_codes=("CAPABILITY_LIMIT",))

    assert result.status is MilpStatus.OPTIMAL
    assert result.optimal_over_explored_graph
    assert not result.globally_optimal_over_discovery_space
    assert result.plan.discovery_complete is False


def test_presolve_false_infeasibility_is_confirmed_on_original_model() -> None:
    """Regression for a HiGHS presolver false-UNSAT on a valid MIP.

    The exact shape and proof-derived column ordering reproduce the failure in
    the deployed SciPy/HiGHS build.  A presolved infeasibility assertion must be
    confirmed with the unpresolved model before becoming a scientific answer.
    """
    r0 = RequirementUseNode(
        "r0", "req:r0", "r0", optional=True,
        default_id="default:r0", cardinality=CardinalityRange(0, 1),
        distinct_by=DistinctBy.PRODUCER, distinctness_group="gp")
    r1 = RequirementUseNode(
        "r1", "req:r1", "r1", cardinality=CardinalityRange(2, 2),
        shareable=False)
    u10 = RequirementUseNode(
        "u10", "req:u10", "u10", owner_invocation_id="i1",
        optional=True, cardinality=CardinalityRange(0, 1),
        distinct_by=DistinctBy.OUTPUT, distinctness_group="go")
    u11 = RequirementUseNode(
        "u11", "req:u11", "u11", owner_invocation_id="i1",
        distinct_by=DistinctBy.PRODUCER, distinctness_group="gp")
    u30 = RequirementUseNode(
        "u30", "req:u30", "u30", owner_invocation_id="i3",
        cardinality=CardinalityRange(1, 2),
        distinct_by=DistinctBy.OUTPUT, distinctness_group="go")
    i0 = _source("i0", 2)
    i1 = InvocationNode("i1", ("u10", "u11"), ("o0",), 5)
    i2 = _source("i2", 0, "o0", "o1")
    i3 = InvocationNode("i3", ("u30",), ("o0",), 5)
    arcs = (
        _arc("u11", "i2", port="o0"),
        _arc("r0", "i2", port="o0"),
        _arc("r1", "i1", port="o0"),
        _arc("r1", "i0", port="result"),
        _arc("r0", "i3", port="o0"),
        _arc("r1", "i3", port="o0"),
        _arc("r0", "i0", port="result"),
        _arc("u11", "i2", port="o1"),
        _arc("r0", "i1", port="o0"),
        _arc("r1", "i2", port="o0"),
        _arc("u11", "i1", port="o0"),
        _arc("u30", "i1", port="o0"),
    )
    graph = OracleProblem.bind(
        "presolve-false-unsat-regression",
        uses=(r0, r1, u10, u11, u30),
        root_use_ids=("r0", "r1"),
        invocations=(i0, i1, i2, i3),
        satisfaction_arcs=arcs,
    )
    request = MilpSelectionProblem.bind(
        graph,
        constraints=SelectionConstraints.bind(include=(
            ProducerSelectionRef(ProducerKind.INVOCATION, "i1"),)),
    )

    oracle = exhaustive_enumerate(graph)
    result = solve_milp(request, options=MilpSolveOptions(presolve=True))

    # The unconstrained oracle's identity can differ, so compare the known
    # constrained optimum rather than plan IDs here.
    assert oracle.status is OracleStatus.OPTIMAL
    assert result.status is MilpStatus.OPTIMAL
    assert result.plan.total_cost_units == 5
    assert set(result.plan.selected_invocation_ids) == {"i1", "i2"}


@pytest.mark.parametrize("seed", range(10))
def test_seeded_small_dags_agree_exactly_with_oracle(seed: int) -> None:
    randomizer = random.Random(seed)
    root_a = RequirementUseNode("root-a", "final-a", "result")
    root_b = RequirementUseNode("root-b", "final-b", "result")
    input_a = RequirementUseNode(
        "input-a", "common-input", "input", owner_invocation_id="consumer-a")
    input_b = RequirementUseNode(
        "input-b", "common-input", "input", owner_invocation_id="consumer-b")
    consumer_a = InvocationNode(
        "consumer-a", (input_a.use_id,), ("result",),
        randomizer.randrange(3))
    consumer_b = InvocationNode(
        "consumer-b", (input_b.use_id,), ("result",),
        randomizer.randrange(3))
    shared = _source("shared", randomizer.randrange(5))
    private_a = _source("private-a", randomizer.randrange(5))
    private_b = _source("private-b", randomizer.randrange(5))
    graph = OracleProblem.bind(
        f"seeded-dag-{seed}",
        uses=(root_a, root_b, input_a, input_b),
        root_use_ids=(root_a.use_id, root_b.use_id),
        invocations=(consumer_a, consumer_b, shared, private_a, private_b),
        satisfaction_arcs=(
            _arc(root_a.use_id, consumer_a.invocation_id),
            _arc(root_b.use_id, consumer_b.invocation_id),
            _arc(input_a.use_id, shared.invocation_id),
            _arc(input_b.use_id, shared.invocation_id),
            _arc(input_a.use_id, private_a.invocation_id),
            _arc(input_b.use_id, private_b.invocation_id),
        ),
    )

    oracle = exhaustive_enumerate(graph)
    selected = _solve(graph)

    assert selected.status is MilpStatus.OPTIMAL
    assert selected.plan.plan_id == oracle.optimal_plan.plan_id


def test_recursive_hypergraph_adapter_preserves_real_deployment_sites() -> None:
    fixture = make_composition_fixture()
    discovered = build_feasible_hypergraph(
        fixture.catalog,
        fixture.deployment_snapshot,
        fixture.root_uses,
    )

    result = solve_milp(MilpSelectionProblem.from_hypergraph(discovered))

    assert result.status is MilpStatus.OPTIMAL
    assert result.discovery_complete
    assert result.plan.total_cost_units == 3
    assert len(result.plan.selected_invocation_ids) == 2
    assert {site for _invocation, site in result.deployment_choices} == {
        "private-node-example-cpu"}


def test_static_site_choice_is_canonical_and_not_a_capacity_decision() -> None:
    root = RequirementUseNode("root", "value", "result")
    source = _source("source", 1)
    graph = OracleProblem.bind(
        "canonical-site-choice",
        uses=(root,),
        root_use_ids=(root.use_id,),
        invocations=(source,),
        satisfaction_arcs=(_arc(root.use_id, source.invocation_id),),
        snapshot_refs=(PlanSnapshotRef(
            "deployment_feasibility", "snapshot:synthetic"),),
    )
    options = (
        DeploymentOption("source", "site-z"),
        DeploymentOption("source", "site-a"),
    )

    result = solve_milp(MilpSelectionProblem.bind(
        graph, deployment_options=reversed(options)))

    assert result.status is MilpStatus.OPTIMAL
    assert result.deployment_choices == (("source", "site-a"),)


def test_interactive_solver_has_a_bounded_default_and_offline_can_opt_out():
    assert MilpSolveOptions().time_limit_s == 30.0
    assert MilpSolveOptions(time_limit_s=None).time_limit_s is None


def test_solver_limit_without_incumbent_is_not_reported_as_error_or_unsat(
        monkeypatch) -> None:
    import scipy.optimize

    root = RequirementUseNode("root", "value", "result")
    source = _source("source", 1)
    graph = OracleProblem.bind(
        "limit-no-incumbent",
        uses=(root,),
        root_use_ids=(root.use_id,),
        invocations=(source,),
        satisfaction_arcs=(_arc(root.use_id, source.invocation_id),),
    )
    monkeypatch.setattr(
        scipy.optimize,
        "milp",
        lambda *args, **kwargs: SimpleNamespace(
            status=1,
            x=None,
            message="time limit",
            mip_gap=None,
            mip_node_count=0,
        ),
    )

    result = solve_milp(
        MilpSelectionProblem.bind(graph),
        options=MilpSolveOptions(time_limit_s=None),
    )

    assert result.status is MilpStatus.LIMIT_NO_INCUMBENT
    assert result.plan is None
    assert not result.primary_cost_proven
    assert {value.code for value in result.blockers} == {
        "NO_VALID_INCUMBENT"}


def test_tie_limit_returns_validated_primary_optimum_and_primary_gap(
        monkeypatch) -> None:
    import scipy.optimize

    original = scipy.optimize.milp
    calls = 0
    primary = None

    def limited_after_primary(*args, **kwargs):
        nonlocal calls, primary
        calls += 1
        if calls == 1:
            primary = original(*args, **kwargs)
            return primary
        return SimpleNamespace(
            status=1,
            x=primary.x,
            message="secondary tie limit",
            mip_gap=0.5,
            mip_node_count=0,
        )

    monkeypatch.setattr(scipy.optimize, "milp", limited_after_primary)
    root = RequirementUseNode("root", "value", "result")
    first = _source("first", 1)
    second = _source("second", 1)
    graph = OracleProblem.bind(
        "tie-limit-incumbent",
        uses=(root,),
        root_use_ids=(root.use_id,),
        invocations=(first, second),
        satisfaction_arcs=(
            _arc(root.use_id, first.invocation_id),
            _arc(root.use_id, second.invocation_id),
        ),
    )

    result = solve_milp(
        MilpSelectionProblem.bind(graph),
        options=MilpSolveOptions(time_limit_s=None),
    )

    assert result.status is MilpStatus.FEASIBLE_NOT_PROVEN_OPTIMAL
    assert result.plan is not None
    assert result.plan.total_cost_units == 1
    assert result.primary_cost_proven
    assert not result.tie_break_complete
    assert result.mip_gap in (None, 0.0)
