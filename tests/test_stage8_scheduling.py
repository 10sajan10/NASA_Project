"""Stage-8 resource-aware scheduling: ordering, reservations, and honesty.

The exit gates under test are that CPU and memory are never oversubscribed,
that low-priority work cannot starve, that event-driven execution beats the
layer runner on an imbalanced graph, and that a resource underestimate becomes
a recorded revision rather than a silent mutation.
"""
from __future__ import annotations

import pytest

from scheduling import (
    DeploymentRevision,
    EstimateSource,
    ObservationKind,
    ExecutionSite,
    ObservationHistory,
    OversubscriptionError,
    PriorityPolicy,
    ReservationLedger,
    ResourceEnvelopeSpec,
    SchedulableTask,
    ScheduledNode,
    SchedulingPolicy,
    TaskObservation,
    best_fit_site,
    compare_policies,
    critical_path_ranks,
    detect_capacity,
    order_ready_tasks,
    simulate_schedule,
    thread_environment,
    topological_order,
)
from scheduling.resources import THREAD_ENVIRONMENT_VARIABLES
from stage8 import fixtures as fx
from stage8.demo import run_demo


# -- reservations and capacity -------------------------------------------


def _sites() -> tuple[ExecutionSite, ...]:
    return (ExecutionSite("a", ResourceEnvelopeSpec(4, 4096)),
            ExecutionSite("b", ResourceEnvelopeSpec(8, 8192),
                          environment_classes=("gdal",)))


def test_cpu_and_memory_are_never_oversubscribed():
    ledger = ReservationLedger(_sites())
    ledger.reserve("t1", "a", ResourceEnvelopeSpec(3, 3072))
    assert ledger.invariant_holds()

    with pytest.raises(OversubscriptionError) as cpu_error:
        ledger.reserve("t2", "a", ResourceEnvelopeSpec(2, 512))
    assert cpu_error.value.dimension == "cpu_cores"

    with pytest.raises(OversubscriptionError) as memory_error:
        ledger.reserve("t3", "a", ResourceEnvelopeSpec(1, 2048))
    assert memory_error.value.dimension == "memory_mb"

    # A refused reservation left nothing behind.
    assert ledger.live_task_keys() == ("t1",)
    assert ledger.invariant_holds()


def test_gpu_and_scratch_are_tracked_too():
    ledger = ReservationLedger((
        ExecutionSite("g", ResourceEnvelopeSpec(8, 8192, gpus=1,
                                                scratch_mb=1024)),))
    ledger.reserve("t1", "g", ResourceEnvelopeSpec(1, 128, gpus=1))
    with pytest.raises(OversubscriptionError) as error:
        ledger.reserve("t2", "g", ResourceEnvelopeSpec(1, 128, gpus=1))
    assert error.value.dimension == "gpus"
    with pytest.raises(OversubscriptionError) as scratch:
        ledger.reserve("t3", "g", ResourceEnvelopeSpec(1, 128,
                                                       scratch_mb=2048))
    assert scratch.value.dimension == "scratch_mb"


def test_releasing_a_reservation_returns_the_capacity():
    ledger = ReservationLedger(_sites())
    ledger.reserve("t1", "a", ResourceEnvelopeSpec(4, 4096))
    assert ledger.available("a").cpu_cores == 0
    ledger.release("t1")
    assert ledger.available("a").cpu_cores == 4
    with pytest.raises(KeyError):
        ledger.release("t1")


def test_a_task_larger_than_any_site_is_refused_outright():
    ledger = ReservationLedger(_sites())
    with pytest.raises(OversubscriptionError):
        ledger.reserve("huge", "a", ResourceEnvelopeSpec(64, 128))
    assert best_fit_site(ledger, ResourceEnvelopeSpec(64, 128)) is None


def test_best_fit_prefers_the_tightest_feasible_site():
    ledger = ReservationLedger(_sites())
    # Both fit, but 'a' leaves less slack.
    assert best_fit_site(ledger, ResourceEnvelopeSpec(4, 1024)) == "a"
    # Only 'b' is large enough.
    assert best_fit_site(ledger, ResourceEnvelopeSpec(6, 1024)) == "b"


def test_environment_affinity_restricts_placement():
    ledger = ReservationLedger(_sites())
    assert best_fit_site(ledger, ResourceEnvelopeSpec(1, 128),
                         required_environments=("gdal",)) == "b"
    assert best_fit_site(ledger, ResourceEnvelopeSpec(1, 128),
                         required_environments=("cuda",)) is None


def test_nested_thread_pools_are_capped_to_reserved_cores():
    environment = thread_environment(ResourceEnvelopeSpec(3, 512))
    for name in THREAD_ENVIRONMENT_VARIABLES:
        assert environment[name] == "3"
    # An existing environment is extended, not replaced.
    merged = thread_environment(ResourceEnvelopeSpec(1, 128), {"PATH": "/bin"})
    assert merged["PATH"] == "/bin" and merged["OMP_NUM_THREADS"] == "1"


def test_detected_capacity_is_allocation_aware():
    capacity = detect_capacity(memory_mb=2048)
    assert capacity.cpu_cores >= 1
    assert capacity.memory_mb == 2048
    # Never reports more than the process may actually use.
    import os
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):     # pragma: no cover - platform guard
        affinity = os.cpu_count() or 1
    assert capacity.cpu_cores <= affinity


# -- priority ------------------------------------------------------------


def test_critical_path_rank_is_the_longest_remaining_chain():
    nodes = [
        ScheduledNode("a", 1.0),
        ScheduledNode("b", 10.0, ("a",)),
        ScheduledNode("c", 2.0, ("a",)),
        ScheduledNode("d", 3.0, ("b", "c")),
    ]
    ranks = critical_path_ranks(nodes)
    assert ranks["d"] == 3.0
    assert ranks["b"] == 13.0          # itself plus d
    assert ranks["c"] == 5.0
    assert ranks["a"] == 14.0          # a -> b -> d


def test_a_cycle_is_refused_rather_than_ranked():
    nodes = [ScheduledNode("a", 1.0, ("b",)), ScheduledNode("b", 1.0, ("a",))]
    with pytest.raises(ValueError, match="cycle"):
        critical_path_ranks(nodes)


def test_a_dependency_on_an_unknown_task_is_refused():
    with pytest.raises(KeyError):
        critical_path_ranks([ScheduledNode("a", 1.0, ("ghost",))])


def test_ordering_prefers_critical_path_then_ages_then_task_id():
    policy = PriorityPolicy(aging_weight_per_s=1.0)
    ranks = {"low": 1.0, "high": 100.0}
    assert order_ready_tasks(("low", "high"), ranks, {"low": 0, "high": 0},
                             policy)[0] == "high"
    # Enough waiting flips the order: this is what prevents starvation.
    assert order_ready_tasks(("low", "high"), ranks,
                             {"low": 200.0, "high": 0.0}, policy)[0] == "low"


def test_aging_is_capped_so_it_cannot_invert_the_graph_forever():
    policy = PriorityPolicy(aging_weight_per_s=1.0, starvation_ceiling_s=10.0)
    # Waiting far beyond the ceiling adds no more than the ceiling allows.
    assert policy.score(0.0, 10_000.0) == 10.0
    assert policy.score(5.0, 3.0) == 8.0
    with pytest.raises(ValueError):
        policy.score(0.0, -1.0)


def test_topological_order_is_deterministic():
    nodes = {key: ScheduledNode(key, 1.0, deps) for key, deps in (
        ("c", ("a",)), ("a", ()), ("b", ("a",)), ("d", ("b", "c")))}
    assert topological_order(nodes) == ("a", "b", "c", "d")


# -- the schedule --------------------------------------------------------


def test_event_driven_beats_the_layer_runner_on_the_imbalanced_graph():
    fixture = fx.make_imbalanced_graph()
    results = compare_policies(fixture.tasks, fixture.sites)
    event = results[SchedulingPolicy.EVENT_DRIVEN.value]
    layered = results[SchedulingPolicy.LAYERED.value]
    fifo = results[SchedulingPolicy.FIFO.value]

    assert event.makespan_s < layered.makespan_s
    assert event.makespan_s < fifo.makespan_s
    # And it is not merely better; it hits the critical-path lower bound, so
    # no ordering could do better on this graph.
    assert event.makespan_s == pytest.approx(fixture.critical_path_s)


def test_a_downstream_task_starts_the_moment_its_dependency_commits():
    fixture = fx.make_imbalanced_graph()
    result = simulate_schedule(fixture.tasks, fixture.sites,
                               policy=SchedulingPolicy.EVENT_DRIVEN)
    chain = [result.start_of(key) for key in fixture.chain_keys]
    for previous, following in zip(chain, chain[1:]):
        # No gap: it did not wait behind unrelated short work.
        assert following.start_s == pytest.approx(previous.finish_s)


def test_no_policy_ever_oversubscribes_the_node():
    fixture = fx.make_imbalanced_graph()
    for result in compare_policies(fixture.tasks, fixture.sites).values():
        assert result.oversubscribed is False
        peak = result.peak_usage["private-node"]
        assert peak["cpu_cores"] <= fx.NODE_CORES
        assert peak["memory_mb"] <= fx.NODE_MEMORY_MB


def test_every_task_runs_exactly_once_under_every_policy():
    fixture = fx.make_imbalanced_graph()
    expected = {task.task_key for task in fixture.tasks}
    for result in compare_policies(fixture.tasks, fixture.sites).values():
        keys = [item.task_key for item in result.starts]
        assert sorted(keys) == sorted(expected)
        assert len(keys) == len(set(keys))


def test_dependencies_are_always_respected():
    fixture = fx.make_imbalanced_graph()
    by_key = {task.task_key: task for task in fixture.tasks}
    for result in compare_policies(fixture.tasks, fixture.sites).values():
        finish = {item.task_key: item.finish_s for item in result.starts}
        for item in result.starts:
            for parent in by_key[item.task_key].dependencies:
                assert finish[parent] <= item.start_s + 1e-9


def test_aging_rescues_a_task_that_would_otherwise_starve():
    fixture = fx.make_starvation_graph()
    without = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        priority_policy=PriorityPolicy(aging_weight_per_s=0.0))
    with_aging = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        priority_policy=PriorityPolicy(aging_weight_per_s=5.0))

    lonely = "lonely-small-task"
    assert without.start_of(lonely).start_s > with_aging.start_of(lonely).start_s
    # On a single core the total work is fixed, so anti-starvation is free here.
    assert without.makespan_s == pytest.approx(with_aging.makespan_s)


def test_work_that_fits_nowhere_deadlocks_loudly():
    task = SchedulableTask("huge", ResourceEnvelopeSpec(64, 128), 1.0)
    site = ExecutionSite("small", ResourceEnvelopeSpec(2, 1024))
    with pytest.raises(RuntimeError, match="deadlocked"):
        simulate_schedule((task,), (site,))


def test_scheduling_nothing_is_refused():
    with pytest.raises(ValueError, match="nothing to schedule"):
        simulate_schedule((), (ExecutionSite("a", ResourceEnvelopeSpec(1, 128)),))


# -- observations --------------------------------------------------------


def test_declared_estimates_are_used_until_there_is_enough_history():
    history = ObservationHistory(minimum_samples=3)
    assert history.duration_estimate("t", 10.0).source is EstimateSource.DECLARED

    history.record_all([TaskObservation.completed("t", 4.0, memory_mb=100),
                        TaskObservation.completed("t", 4.5, memory_mb=110)])
    below = history.duration_estimate("t", 10.0)
    assert below.source is EstimateSource.DECLARED and below.value == 10.0
    assert below.sample_count == 2

    history.record(TaskObservation.completed("t", 4.2, memory_mb=120))
    enough = history.duration_estimate("t", 10.0)
    assert enough.source is EstimateSource.OBSERVED
    assert enough.value == pytest.approx(4.2)      # median, not mean


def test_failed_attempts_do_not_make_a_task_look_fast():
    history = ObservationHistory(minimum_samples=2)
    history.record_all([
        TaskObservation("t", 0.1, ResourceEnvelopeSpec(1, 10),
                        ObservationKind.FAILED),
        TaskObservation("t", 0.1, ResourceEnvelopeSpec(1, 10),
                        ObservationKind.FAILED),
        TaskObservation.completed("t", 9.0, memory_mb=100),
    ])
    # Only one successful sample, so the declared estimate still stands.
    assert history.duration_estimate("t", 10.0).source is EstimateSource.DECLARED
    assert history.failure_rate("t").value == pytest.approx(2 / 3)


def test_memory_estimates_take_the_worst_observed_case():
    history = ObservationHistory(minimum_samples=2)
    history.record_all([TaskObservation.completed("t", 1.0, memory_mb=100),
                        TaskObservation.completed("t", 1.0, memory_mb=400)])
    assert history.memory_estimate("t", 256).value == pytest.approx(400.0)


def test_an_underestimate_becomes_a_recorded_revision_not_a_mutation():
    history = ObservationHistory(minimum_samples=1)
    declared = ResourceEnvelopeSpec(cpu_cores=2, memory_mb=256)
    history.record(TaskObservation.completed("t", 1.0, memory_mb=900))
    revision = history.review_envelope("t", declared)

    assert isinstance(revision, DeploymentRevision)
    assert revision.observed_peak_memory_mb == 900
    # The declared envelope is preserved in the record and left unmodified;
    # the revision only *proposes* a larger one.
    assert revision.declared.memory_mb == 256
    assert revision.proposed.memory_mb > 900
    assert revision.proposed.cpu_cores == declared.cpu_cores
    assert declared.memory_mb == 256
    assert history.revisions == (revision,)
    assert revision.revision_id == revision.expected_id()


def test_no_revision_when_the_declared_envelope_held():
    history = ObservationHistory(minimum_samples=1)
    history.record(TaskObservation.completed("t", 1.0, memory_mb=100))
    assert history.review_envelope(
        "t", ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256)) is None


def test_measured_history_feeds_back_into_the_schedule():
    fixture = fx.make_imbalanced_graph()
    history = ObservationHistory(minimum_samples=1)
    for key in fixture.chain_keys:
        history.record(TaskObservation.completed(key, 2.0, memory_mb=100))
    faster = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN,
        history=history)
    declared = simulate_schedule(
        fixture.tasks, fixture.sites, policy=SchedulingPolicy.EVENT_DRIVEN)
    assert faster.makespan_s < declared.makespan_s


# -- the demonstration ---------------------------------------------------


def test_the_demo_meets_every_stage_8_exit_gate():
    result = run_demo()

    graph = result["imbalanced_graph"]
    assert graph["beats_layer_runner"] is True
    assert graph["matches_critical_path_bound"] is True
    assert graph["chain_runs_back_to_back"] is True
    assert graph["oversubscribed"] is False
    assert graph["improvement_percent"] > 20

    assert result["starvation"]["aging_reduces_wait"] is True
    assert result["starvation"]["total_work_is_conserved"] is True

    reservations = result["reservations"]
    assert reservations["refused_dimension"] == "cpu_cores"
    assert reservations["invariant_holds"] is True
    assert reservations["environment_affinity_site"] == "gdal-node"
    assert reservations["thread_caps"]["OMP_NUM_THREADS"] == "2"

    observations = result["observations"]
    assert observations["declared_used_until_enough_history"] is True
    assert observations["underestimate_recorded"] is True
    assert observations["revision_is_a_proposal_not_a_mutation"] is True


# -- audit findings -------------------------------------------------------


def test_logical_sites_cannot_jointly_overrun_their_physical_host():
    """Two 8-core logical sites on one 8-core machine is oversubscription.

    The audit scheduled two eight-core tasks across two logical eight-core
    sites on a single eight-core host and the simulator reported
    oversubscribed=False, because capacity was only ever checked per site.
    """
    host = ResourceEnvelopeSpec(8, 16384)
    ledger = ReservationLedger((
        ExecutionSite("a", ResourceEnvelopeSpec(8, 8192),
                      host_id="node1", host_capacity=host),
        ExecutionSite("b", ResourceEnvelopeSpec(8, 8192),
                      host_id="node1", host_capacity=host),
    ))
    ledger.reserve("t1", "a", ResourceEnvelopeSpec(8, 4096))
    with pytest.raises(OversubscriptionError) as error:
        ledger.reserve("t2", "b", ResourceEnvelopeSpec(8, 4096))
    assert error.value.site_id == "node1"
    assert error.value.dimension == "cpu_cores"
    assert ledger.invariant_holds()


def test_sites_must_agree_about_their_shared_host():
    with pytest.raises(ValueError, match="disagree about the capacity"):
        ReservationLedger((
            ExecutionSite("a", ResourceEnvelopeSpec(4, 1024), host_id="n",
                          host_capacity=ResourceEnvelopeSpec(8, 8192)),
            ExecutionSite("b", ResourceEnvelopeSpec(4, 1024), host_id="n",
                          host_capacity=ResourceEnvelopeSpec(16, 8192)),
        ))
    with pytest.raises(ValueError, match="host capacity requires a host_id"):
        ExecutionSite("a", ResourceEnvelopeSpec(4, 1024),
                      host_capacity=ResourceEnvelopeSpec(8, 8192))


def test_placement_preserves_scarce_accelerators_for_work_that_needs_them():
    """Best fit weighed only CPU and memory, so CPU work took the GPU node."""
    ledger = ReservationLedger((
        ExecutionSite("plain", ResourceEnvelopeSpec(4, 4096)),
        ExecutionSite("gpu", ResourceEnvelopeSpec(4, 4096, gpus=2)),
    ))
    assert best_fit_site(ledger, ResourceEnvelopeSpec(2, 1024)) == "plain"
    assert best_fit_site(
        ledger, ResourceEnvelopeSpec(2, 1024, gpus=1)) == "gpu"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_durations_are_refused_everywhere(value):
    """NaN passes every "< 0" test and then poisons the simulator's min/max."""
    with pytest.raises(ValueError, match="finite"):
        SchedulableTask("x", ResourceEnvelopeSpec(1, 128), value)
    with pytest.raises(ValueError, match="finite"):
        ScheduledNode("x", value)
    with pytest.raises(ValueError, match="finite"):
        TaskObservation.completed("x", value, memory_mb=10)
    with pytest.raises(ValueError, match="finite"):
        PriorityPolicy(aging_weight_per_s=value)


def test_an_attempt_killed_by_a_limit_still_drives_a_revision():
    """The OOM is the most informative evidence and was being discarded."""
    history = ObservationHistory(minimum_samples=1)
    declared = ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256)
    history.record(TaskObservation.resource_exhausted(
        "t", 1.0, dimension="memory_mb", memory_mb=900))

    revision = history.review_envelope("t", declared)
    assert revision is not None
    assert revision.dimensions == ("memory_mb",)
    assert revision.proposed.memory_mb > 900
    # The proposal rests on a censored lower bound, and says so: the attempt
    # was stopped, so the true requirement may be higher still.
    assert revision.from_censored_evidence is True
    assert "may be higher still" in revision.reason
    assert declared.memory_mb == 256          # still not mutated


def test_revisions_cover_every_dimension_not_only_memory():
    history = ObservationHistory(minimum_samples=1)
    declared = ResourceEnvelopeSpec(cpu_cores=2, memory_mb=4096, gpus=0,
                                    scratch_mb=0)
    history.record(TaskObservation.completed(
        "t", 1.0, memory_mb=100, cpu_cores=8, gpus=2, scratch_mb=500))

    revision = history.review_envelope("t", declared)
    assert revision is not None
    assert revision.dimensions == ("cpu_cores", "gpus", "scratch_mb")
    # Cores and GPUs are discrete counts: propose exactly what was used rather
    # than padding to hardware nobody asked for.
    assert revision.proposed.cpu_cores == 8
    assert revision.proposed.gpus == 2
    assert revision.proposed.scratch_mb > 500
    assert revision.from_censored_evidence is False


def test_a_plain_failure_is_not_treated_as_resource_evidence():
    history = ObservationHistory(minimum_samples=1)
    history.record(TaskObservation(
        "t", 1.0, ResourceEnvelopeSpec(1, 900), ObservationKind.FAILED))
    # A task that crashed for its own reasons says nothing about sizing.
    assert history.review_envelope(
        "t", ResourceEnvelopeSpec(1, 256)) is None


def test_a_resource_exhausted_observation_must_name_its_dimension():
    with pytest.raises(ValueError, match="must name the dimension"):
        TaskObservation("t", 1.0, ResourceEnvelopeSpec(1, 900),
                        ObservationKind.RESOURCE_EXHAUSTED)
    with pytest.raises(ValueError, match="only a resource-exhausted"):
        TaskObservation("t", 1.0, ResourceEnvelopeSpec(1, 900),
                        ObservationKind.COMPLETED,
                        exhausted_dimension="memory_mb")


def test_capped_aging_bounds_delay_but_does_not_guarantee_service():
    """State the real property rather than the stronger one we cannot prove.

    With a capped bonus, a rank gap wider than ceiling x weight can never be
    overcome, so "low-priority work cannot starve" is only true for gaps
    within that bound. The cap is still wanted -- without it the policy
    degenerates to FIFO -- so the honest claim is bounded delay.
    """
    policy = PriorityPolicy(aging_weight_per_s=1.0, starvation_ceiling_s=10.0)
    max_bonus = policy.starvation_ceiling_s * policy.aging_weight_per_s

    # A gap inside the bound is eventually overcome.
    inside = order_ready_tasks(
        ("low", "high"), {"low": 0.0, "high": max_bonus - 1},
        {"low": 10_000.0, "high": 0.0}, policy)
    assert inside[0] == "low"

    # A gap outside it never is, no matter how long the wait.
    outside = order_ready_tasks(
        ("low", "high"), {"low": 0.0, "high": max_bonus + 1},
        {"low": 10_000_000.0, "high": 0.0}, policy)
    assert outside[0] == "high"
