"""The runtime bridge: Stage-8 policy driving the durable Stage-1 controller.

Stages 7 and 8 were built as layers *around* a controller that was
deliberately serial, so their central claims sat next to the runtime rather
than inside it. These tests exercise the real thing: concurrent attempts
through the durable controller, running real subprocesses, committing real
artifacts, under a reservation ledger that refuses to oversubscribe.

Makespan here is wall clock, not simulation.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from engine.runtime import RunState, WorkflowController
from engine.runtime.operations import operation_component
from engine.runtime.types import (
    BoundExecutionGraph,
    OutputSpec,
    ResourceRequest,
    TaskTemplate,
)
from scheduling import (
    ExecutionSite,
    ObservationHistory,
    PriorityPolicy,
    ReservationLedger,
    ResourceEnvelopeSpec,
)

SLEEP = operation_component("synthetic.sleep.v1")
TASK_SECONDS = 0.4


def _graph(count: int, seconds: float = TASK_SECONDS) -> BoundExecutionGraph:
    """``count`` independent sleeps: pure width, so concurrency is visible."""
    return BoundExecutionGraph.bind("bridge", tuple(
        TaskTemplate(
            key=f"t{index}", component=SLEEP,
            parameters={"seconds": seconds, "value": index},
            outputs=(OutputSpec(),),
            resources=ResourceRequest(cpu_cores=1, memory_mb=64,
                                      walltime_s=60))
        for index in range(count)))


def _ledger(cores: int) -> ReservationLedger:
    return ReservationLedger([
        ExecutionSite("node", ResourceEnvelopeSpec(cores, 4096))])


def _run(root: Path, count: int, inflight: int, *,
         observations: ObservationHistory | None = None) -> tuple[float, RunState]:
    extra: dict = {}
    if inflight > 1:
        extra = dict(ledger=_ledger(inflight), site_id="node",
                     priority_policy=PriorityPolicy(),
                     observations=observations)
    started = time.monotonic()
    with WorkflowController(root, max_inflight=inflight,
                            poll_interval_s=0.01, **extra) as controller:
        run_id = controller.create_run(_graph(count))
        state = controller.run_until_terminal(run_id, timeout_s=180)
    return time.monotonic() - started, state


def test_concurrency_requires_a_reservation_ledger(tmp_path):
    """Running N attempts blind is the oversubscription the ledger prevents."""
    with pytest.raises(ValueError, match="requires a ReservationLedger"):
        WorkflowController(tmp_path / "a", max_inflight=4)
    with pytest.raises(ValueError, match="positive integer"):
        WorkflowController(tmp_path / "b", max_inflight=-1)


def test_concurrent_attempts_beat_serial_wall_clock(tmp_path):
    """A real makespan improvement, measured on real subprocesses."""
    serial, serial_state = _run(tmp_path / "serial", 8, 1)
    concurrent, concurrent_state = _run(tmp_path / "concurrent", 8, 4)

    assert serial_state is RunState.SUCCEEDED
    assert concurrent_state is RunState.SUCCEEDED
    # Eight 0.4s tasks: serial is bounded below by 3.2s, four-wide by 0.8s.
    # A generous factor keeps this robust on a loaded shared node while still
    # failing if concurrency silently stops happening.
    assert concurrent < serial * 0.7


def test_every_task_still_commits_under_concurrency(tmp_path):
    """Concurrency must not cost correctness: same results, same commits."""
    with WorkflowController(tmp_path / "run", max_inflight=4,
                            poll_interval_s=0.01, ledger=_ledger(4),
                            site_id="node") as controller:
        graph = _graph(6)
        run_id = controller.create_run(graph)
        state = controller.run_until_terminal(run_id, timeout_s=180)
        assert state is RunState.SUCCEEDED
        values = {
            task.key: controller.output_value(run_id, task.task_id, "result")
            for task in graph.tasks
        }
        with controller.store.connect() as connection:
            attempts = int(connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id=?",
                (run_id,)).fetchone()[0])
    assert values == {f"t{index}": index for index in range(6)}
    # Exactly one attempt per task: concurrency did not cause re-execution.
    assert attempts == 6


def test_the_ledger_bounds_real_concurrency(tmp_path):
    """Capacity, not max_inflight alone, decides how much actually runs."""
    ledger = _ledger(2)
    observed_peak = 0
    with WorkflowController(tmp_path / "run", max_inflight=8,
                            poll_interval_s=0.01, ledger=ledger,
                            site_id="node") as controller:
        run_id = controller.create_run(_graph(8))
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = controller.tick(run_id)
            observed_peak = max(observed_peak,
                                ledger.used("node").cpu_cores)
            assert ledger.invariant_holds()
            if state in (RunState.SUCCEEDED, RunState.FAILED,
                         RunState.CANCELLED):
                break
            time.sleep(0.01)
    assert state is RunState.SUCCEEDED
    # The two-core ledger caps it at two even though eight were permitted.
    assert 0 < observed_peak <= 2


def test_worker_attempts_produce_real_observations(tmp_path):
    """Observations come from actual attempts rather than being injected."""
    history = ObservationHistory(minimum_samples=1)
    _wall, state = _run(tmp_path / "run", 4, 4, observations=history)
    assert state is RunState.SUCCEEDED

    samples = [history.sample_count(f"t{index}") for index in range(4)]
    assert all(count >= 1 for count in samples)
    estimate = history.duration_estimate("t0", declared_s=99.0)
    assert estimate.measured
    # A real sleep task takes roughly its sleep, not the declared 99s.
    assert 0.0 < estimate.value < 30.0


def test_reservations_are_all_released_when_the_run_finishes(tmp_path):
    ledger = _ledger(4)
    with WorkflowController(tmp_path / "run", max_inflight=4,
                            poll_interval_s=0.01, ledger=ledger,
                            site_id="node") as controller:
        run_id = controller.create_run(_graph(6))
        assert controller.run_until_terminal(
            run_id, timeout_s=180) is RunState.SUCCEEDED
    # Nothing leaked: the node is fully free again.
    assert ledger.live_task_keys() == ()
    assert ledger.available("node").cpu_cores == 4
