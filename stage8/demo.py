"""Runnable Stage-8 demonstration: ordering, reservations, and honesty.

Five things are shown, each an exit gate:

1. event-driven scheduling beats the layer runner on an imbalanced graph, and
   a downstream task starts the instant its last dependency commits;
2. CPU and memory are never oversubscribed, because the ledger refuses;
3. low-priority work does not starve, because aging eventually outranks a long
   critical path;
4. a resource underestimate produces a recorded `DeploymentRevision`, never a
   silent widening; and
5. declared estimates are used until there is enough measured history, and the
   estimate says which it is.
"""
from __future__ import annotations

from typing import Any

from scheduling import (
    ObservationHistory,
    OversubscriptionError,
    PriorityPolicy,
    ReservationLedger,
    ResourceEnvelopeSpec,
    SchedulingPolicy,
    TaskObservation,
    best_fit_site,
    compare_policies,
    detect_capacity,
    simulate_schedule,
    thread_environment,
)

from . import fixtures as fx


def _makespans(fixture: fx.Stage8Fixture) -> dict[str, Any]:
    results = compare_policies(fixture.tasks, fixture.sites)
    event = results[SchedulingPolicy.EVENT_DRIVEN.value]
    layered = results[SchedulingPolicy.LAYERED.value]
    fifo = results[SchedulingPolicy.FIFO.value]
    chain_starts = [event.start_of(key) for key in fixture.chain_keys]
    # Each link begins exactly when the previous one finished: no waiting on
    # unrelated short work.
    back_to_back = all(
        abs(chain_starts[index].start_s - chain_starts[index - 1].finish_s) < 1e-9
        for index in range(1, len(chain_starts)))
    return {
        "layered_makespan_s": layered.makespan_s,
        "fifo_makespan_s": fifo.makespan_s,
        "event_driven_makespan_s": event.makespan_s,
        "critical_path_lower_bound_s": fixture.critical_path_s,
        "improvement_over_layered_s": layered.makespan_s - event.makespan_s,
        "improvement_percent": round(
            100 * (layered.makespan_s - event.makespan_s) / layered.makespan_s, 1),
        "beats_layer_runner": event.makespan_s < layered.makespan_s,
        "matches_critical_path_bound": (
            abs(event.makespan_s - fixture.critical_path_s) < 1e-9),
        "chain_runs_back_to_back": back_to_back,
        "chain_timeline": [item.to_dict() for item in chain_starts],
        "peak_usage": event.peak_usage,
        "oversubscribed": event.oversubscribed,
    }


def _starvation() -> dict[str, Any]:
    fixture = fx.make_starvation_graph()
    without_aging = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        priority_policy=PriorityPolicy(aging_weight_per_s=0.0))
    with_aging = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        priority_policy=PriorityPolicy(aging_weight_per_s=5.0,
                                       starvation_ceiling_s=300.0))
    lonely = "lonely-small-task"
    return {
        "task": lonely,
        "start_without_aging_s": without_aging.start_of(lonely).start_s,
        "start_with_aging_s": with_aging.start_of(lonely).start_s,
        "aging_reduces_wait": (with_aging.start_of(lonely).start_s
                               < without_aging.start_of(lonely).start_s),
        "makespan_without_aging_s": without_aging.makespan_s,
        "makespan_with_aging_s": with_aging.makespan_s,
        "total_work_is_conserved": (
            abs(with_aging.makespan_s - without_aging.makespan_s) < 1e-9),
    }


def _reservations() -> dict[str, Any]:
    sites = fx.make_affinity_sites()
    ledger = ReservationLedger(sites)
    ledger.reserve("big", "plain-node",
                   ResourceEnvelopeSpec(cpu_cores=7, memory_mb=1024))
    refused: str | None = None
    try:
        ledger.reserve("too-big", "plain-node",
                       ResourceEnvelopeSpec(cpu_cores=2, memory_mb=1024))
    except OversubscriptionError as error:
        refused = error.dimension
    gdal_site = best_fit_site(
        ledger, ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256),
        required_environments=("gdal",))
    plain_site = best_fit_site(
        ledger, ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256))
    return {
        "refused_dimension": refused,
        "invariant_holds": ledger.invariant_holds(),
        "environment_affinity_site": gdal_site,
        "best_fit_site_for_small_work": plain_site,
        "thread_caps": thread_environment(
            ResourceEnvelopeSpec(cpu_cores=2, memory_mb=512)),
        "detected_capacity": detect_capacity(memory_mb=4096).to_dict(),
    }


def _observations() -> dict[str, Any]:
    history = ObservationHistory(minimum_samples=3)
    declared = ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256)
    key = "z-chain-1"

    first = history.duration_estimate(key, fx.CHAIN_DURATION_S)
    history.record_all([
        TaskObservation(key, duration_s=4.0, peak_memory_mb=300),
        TaskObservation(key, duration_s=4.5, peak_memory_mb=310),
    ])
    still_declared = history.duration_estimate(key, fx.CHAIN_DURATION_S)
    history.record(TaskObservation(key, duration_s=4.2, peak_memory_mb=320))
    measured = history.duration_estimate(key, fx.CHAIN_DURATION_S)
    revision = history.review_envelope(key, declared)

    # Re-plan with measured history: the chain is cheaper than declared, so the
    # critical path -- and the schedule built on it -- both shrink.
    fixture = fx.make_imbalanced_graph()
    with_history = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        history=history)
    return {
        "estimate_before_any_samples": first.to_dict(),
        "estimate_below_minimum_samples": still_declared.to_dict(),
        "estimate_once_enough_samples": measured.to_dict(),
        "declared_used_until_enough_history": (
            not first.measured and not still_declared.measured
            and measured.measured),
        "underestimate_recorded": revision is not None,
        "revision": revision.to_dict() if revision else None,
        "revision_is_a_proposal_not_a_mutation": (
            revision is not None
            and revision.declared.memory_mb == declared.memory_mb
            and revision.proposed.memory_mb > declared.memory_mb),
        "makespan_with_measured_history_s": with_history.makespan_s,
    }


def run_demo() -> dict[str, Any]:
    fixture = fx.make_imbalanced_graph()
    return {
        "schema": "stage8-demo-result-v1",
        "imbalanced_graph": {
            "short_tasks": fx.SHORT_COUNT,
            "chain_links": fx.CHAIN_LENGTH,
            "node_cores": fx.NODE_CORES,
            **_makespans(fixture),
        },
        "starvation": _starvation(),
        "reservations": _reservations(),
        "observations": _observations(),
    }


__all__ = ["run_demo"]
