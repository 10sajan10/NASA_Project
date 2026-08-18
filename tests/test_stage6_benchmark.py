"""The Section 9.5 planning-latency gate, measured and recorded honestly.

This gate has been carried as "pending" since Stage 3.  Stage 6 measured it on
a representative graph and **it does not pass on this node**: the MILP solve
dominates and exceeds the 5 s p95 budget well inside the node and arc caps.

That result is recorded rather than engineered around.  Shrinking the graph
until the number looked good would have produced a passing gate that meant
nothing.  The latency assertion below is therefore a *strict xfail*: it
documents the miss, and if the solver ever gets fast enough to pass it, the
suite fails and forces someone to re-freeze the profile and update the claim.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage6.benchmark import (
    SECTION_9_5_MAX_ARCS,
    SECTION_9_5_MAX_INVOCATIONS,
    SECTION_9_5_MAX_LEVELS,
    SECTION_9_5_P95_BUDGET_S,
    make_representative_graph,
    representative_resolver,
    section_9_5_report,
)

PROFILE_PATH = (Path(__file__).resolve().parents[1] / "stage6"
                / "planning_benchmark_v1.json")


@pytest.fixture(scope="module")
def frozen() -> dict:
    if not PROFILE_PATH.exists():
        pytest.skip(
            "frozen benchmark absent; run scripts/freeze_stage6_benchmark.py")
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


# -- the representative graph --------------------------------------------


def test_the_representative_graph_offers_real_alternatives():
    graph = make_representative_graph(base_width=8, levels=4)
    # Two competing producers per slot at every level, so selection is not a
    # forced walk down a single chain.
    assert graph.capability_count == 2 * sum(graph.level_widths)
    assert graph.level_widths == (8, 4, 2, 1)


def test_the_representative_graph_resolves_optimally():
    graph = make_representative_graph(base_width=8, levels=4)
    outcome = representative_resolver(graph)()
    assert outcome.status.value == "READY"
    assert outcome.selection.discovery_complete
    assert outcome.selection.globally_optimal_over_discovery_space
    assert outcome.validation is not None and outcome.validation.valid
    # Every level picks the cheaper of its two variants.
    assert outcome.selection.objective_cost_units == 2 * sum(graph.level_widths)


# -- the frozen Section 9.5 measurement ----------------------------------


def test_the_frozen_profile_respects_the_section_9_5_caps(frozen):
    report = frozen["section_9_5"]
    assert report["invocation_count"] <= SECTION_9_5_MAX_INVOCATIONS
    assert report["satisfaction_arc_count"] <= SECTION_9_5_MAX_ARCS
    assert report["selected_derivation_depth"] <= SECTION_9_5_MAX_LEVELS
    assert report["within_node_cap"] and report["within_arc_cap"]
    assert report["within_level_cap"]


def test_the_frozen_profile_is_correctness_gated(frozen):
    report = frozen["section_9_5"]
    # A latency number measured on a wrong or truncated answer is worthless.
    assert report["discovery_complete"] is True
    assert report["globally_optimal"] is True
    assert report["validation_passed"] is True
    assert report["measured_runs"] >= 30


def test_the_frozen_profile_is_a_real_graph_not_a_toy(frozen):
    report = frozen["section_9_5"]
    # Guards against quietly shrinking the graph until the budget passes.
    assert report["invocation_count"] >= 100
    assert report["satisfaction_arc_count"] >= 200
    assert report["selected_derivation_depth"] >= 5


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Section 9.5 p95 <= 5 s is NOT met on this node: 7.06 s at 126 "
        "invocations and 250 arcs, well inside the 1,000-node cap. Stage-8R "
        "took it from 15.12 s (3.02x over) to 1.41x over. The remainder is "
        "one solver call: of 11 calls at this size the primary cost MILP is "
        "4.89 s and the nine lexicographic tie-break chunks total ~0.1 s, so "
        "tie-breaking is NOT the cost. Recorded as "
        "a real miss. A separate and larger limit sits just beyond this "
        "graph: at ~254 invocations the 30 s interactive solve limit expires "
        "before optimality is proven -- there only two solver calls happen "
        "and the primary solve alone consumes the budget -- so the resolver "
        "honestly reports FEASIBLE_NOT_PROVEN_OPTIMAL rather than claiming a "
        "global optimum. Its cause is not yet identified: tightening the rank "
        "domain and big-M 253->16, and breaking source-level cost symmetry, "
        "each changed the time by under 1%. "
        "If this starts passing, re-freeze the profile and update "
        "stage6/README.md and the roadmap rather than deleting this marker."))
def test_section_9_5_latency_budget_is_met(frozen):
    report = frozen["section_9_5"]
    assert report["p95_total_s"] <= SECTION_9_5_P95_BUDGET_S
    assert report["within_latency_budget"] is True


def test_the_miss_is_recorded_with_its_magnitude(frozen):
    report = frozen["section_9_5"]
    assert report["within_latency_budget"] is False
    assert report["p95_budget_s"] == SECTION_9_5_P95_BUDGET_S
    # The overrun is large, not marginal; record it so the next stage sizes
    # the optimisation work against a real number.
    assert report["p95_total_s"] > SECTION_9_5_P95_BUDGET_S


def test_the_section_report_shape_is_stable():
    graph = make_representative_graph(base_width=8, levels=4)
    from resolution import PlanningBenchmarkProfile
    profile = PlanningBenchmarkProfile.measure(
        representative_resolver(graph), warmup_runs=0, measured_runs=1,
        run_config={"benchmark": "stage6-representative-v1-smoke"})
    report = section_9_5_report(profile)
    assert set(report) == {
        "schema", "benchmark_id", "measured_runs", "invocation_count",
        "satisfaction_arc_count", "selected_derivation_depth", "p95_total_s",
        "p95_budget_s", "within_node_cap", "within_arc_cap",
        "within_level_cap", "within_latency_budget", "discovery_complete",
        "globally_optimal", "validation_passed"}
    assert report["schema"] == "stage6-section-9-5-report-v1"
