"""Acceptance tests for the Stage-1 durable local execution kernel.

The suite uses only scalar/JSON synthetic work on the current private node.  It
does not invoke WRF-SFIRE, MPI, SLURM, a network source, or a generic command.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from engine.runtime.artifacts import (
    AFTER_AUTHORITATIVE_COMMIT,
    AFTER_OBJECTS_FINALIZED,
    AFTER_RESULT_PARSED,
    CommitDisposition,
)
from engine.runtime.controller import WorkflowController
from engine.runtime.fixtures import retry_graph, sleeping_graph, two_task_graph
from engine.runtime.operations import operation_component
from engine.runtime.site import current_private_site, validate_runtime_root
from engine.runtime.types import (
    BoundExecutionGraph,
    ResourceRequest,
    RunState,
    TaskState,
    TaskTemplate,
)


class _CrashOnce:
    def __init__(self, boundary: str) -> None:
        self.boundary = boundary
        self.hit = False

    def __call__(self, boundary: str) -> None:
        if boundary == self.boundary and not self.hit:
            self.hit = True
            raise RuntimeError(f"injected controller crash at {boundary}")


def _tick_until(controller: WorkflowController, run_id: str,
                predicate, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        controller.tick(run_id)
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def _attempt_count(controller: WorkflowController, run_id: str) -> int:
    with controller.store.connect() as con:
        return int(con.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
        ).fetchone()[0])


def test_fixed_two_task_graph_commits_before_releasing_consumer(tmp_path):
    crash = _CrashOnce(AFTER_OBJECTS_FINALIZED)
    graph = two_task_graph()
    producer = graph.task_by_key("synthetic-producer")
    consumer = graph.task_by_key("deterministic-consumer")

    controller = WorkflowController(tmp_path, artifact_failpoint=crash)
    run_id = controller.create_run(graph)
    with pytest.raises(RuntimeError, match="injected controller crash"):
        while True:
            controller.tick(run_id)

    assert crash.hit
    assert controller.store.task_state(
        run_id, producer.task_id) is TaskState.VALIDATING
    assert controller.store.task_state(
        run_id, consumer.task_id) is TaskState.WAITING
    assert controller.store.committed_output(
        run_id, producer.task_id) is None
    assert _attempt_count(controller, run_id) == 1
    controller.close()

    with WorkflowController(tmp_path) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert recovered.output_value(run_id, consumer.task_id) == 42
        assert _attempt_count(recovered, run_id) == 2
        assert recovered.store.event_count(
            event_type="ArtifactCommitted") == 2
        with recovered.store.connect() as con:
            assert con.execute(
                "SELECT COUNT(*) FROM artifact_catalog_projection WHERE run_id=?",
                (run_id,),
            ).fetchone()[0] == 2
            assert con.execute(
                "SELECT COUNT(*) FROM catalog_outbox "
                "WHERE run_id=? AND state='PROJECTED'",
                (run_id,),
            ).fetchone()[0] == 2
        assert recovered.store.project_catalog_outbox() == 0


def test_controller_restart_reconciles_live_process_without_resubmit(tmp_path):
    marker = tmp_path / "started"
    graph = sleeping_graph(seconds=0.4, marker=marker, value=17)
    task = graph.tasks[0]
    first = WorkflowController(tmp_path)
    run_id = first.create_run(graph)
    first.tick(run_id)  # dispatch
    deadline = time.monotonic() + 3.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    assert _attempt_count(first, run_id) == 1
    first.close()  # does not kill the supervised attempt

    with WorkflowController(tmp_path) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert recovered.output_value(run_id, task.task_id) == 17
        assert _attempt_count(recovered, run_id) == 1


def test_crash_after_commit_does_not_rerun_committed_task(tmp_path):
    crash = _CrashOnce(AFTER_AUTHORITATIVE_COMMIT)
    graph = sleeping_graph(seconds=0.01, value=5)
    task = graph.tasks[0]
    controller = WorkflowController(tmp_path, artifact_failpoint=crash)
    run_id = controller.create_run(graph)
    with pytest.raises(RuntimeError, match="after_authoritative_commit"):
        while True:
            controller.tick(run_id)

    assert controller.store.task_state(
        run_id, task.task_id) is TaskState.SUCCEEDED
    assert _attempt_count(controller, run_id) == 1
    controller.close()

    with WorkflowController(tmp_path) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert recovered.output_value(run_id, task.task_id) == 5
        assert _attempt_count(recovered, run_id) == 1
        assert recovered.store.event_count(
            event_type="ArtifactCommitted") == 1


def test_retry_timer_survives_controller_restart(tmp_path):
    marker = tmp_path / "fail-once"
    graph = retry_graph(marker=marker, retry_delay_s=0.15, value=9)
    task = graph.tasks[0]
    first = WorkflowController(tmp_path)
    run_id = first.create_run(graph)
    _tick_until(
        first,
        run_id,
        lambda: first.store.task_state(
            run_id, task.task_id) is TaskState.RETRY_WAIT,
    )
    assert _attempt_count(first, run_id) == 1
    first.close()

    with WorkflowController(tmp_path) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert recovered.output_value(run_id, task.task_id) == 9
        assert _attempt_count(recovered, run_id) == 2
        with recovered.store.connect() as con:
            retry_wakes = con.execute(
                "SELECT status FROM wake_conditions WHERE run_id=? AND kind=?",
                (run_id, "RETRY_TIMER"),
            ).fetchall()
        assert [row[0] for row in retry_wakes] == ["CONSUMED"]


def test_invalid_staged_output_is_never_committed(tmp_path):
    crash = _CrashOnce(AFTER_RESULT_PARSED)
    graph = sleeping_graph(seconds=0.01, value=3)
    task = graph.tasks[0]
    controller = WorkflowController(tmp_path, artifact_failpoint=crash)
    run_id = controller.create_run(graph)
    with pytest.raises(RuntimeError, match="after_result_parsed"):
        while True:
            controller.tick(run_id)
    with controller.store.connect() as con:
        stage_dir = Path(con.execute(
            "SELECT stage_dir FROM attempts WHERE run_id=?", (run_id,),
        ).fetchone()[0])
    # Emulate corrupt/partial bytes after provider completion.  The immutable
    # commit boundary must independently reject them.
    (stage_dir / "outputs/result/payload.json").write_text("NaN")
    controller.close()

    with WorkflowController(tmp_path) as recovered:
        assert recovered.run_until_terminal(run_id) is RunState.FAILED
        assert recovered.store.task_state(
            run_id, task.task_id) is TaskState.INVALID_OUTPUT
        assert recovered.store.committed_output(
            run_id, task.task_id) is None
        with recovered.store.connect() as con:
            assert con.execute(
                "SELECT COUNT(*) FROM artifact_commits WHERE run_id=?",
                (run_id,),
            ).fetchone()[0] == 0


def test_duplicate_same_attempt_is_idempotent_and_changed_bytes_conflict(tmp_path):
    graph = sleeping_graph(seconds=0.01, value=8)
    task = graph.tasks[0]
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        with controller.store.connect() as con:
            attempt_id = con.execute(
                "SELECT attempt_id FROM task_commits WHERE run_id=? AND task_id=?",
                (run_id, task.task_id),
            ).fetchone()[0]
        record = controller.store.attempt_record(attempt_id)
        assert controller.committer.process(
            record) is CommitDisposition.DUPLICATE
        assert controller.store.event_count(
            event_type="ArtifactCommitted") == 1

        payload = Path(record.spec.stage_dir) / "outputs/result/payload.json"
        payload.chmod(0o600)
        payload.write_text("999")
        assert controller.committer.process(
            record) is CommitDisposition.CONFLICT
        assert controller.output_value(run_id, task.task_id) == 8


def test_cancellation_fences_process_and_publishes_nothing(tmp_path):
    marker = tmp_path / "cancel-started"
    graph = sleeping_graph(seconds=5, marker=marker, value=1)
    task = graph.tasks[0]
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        controller.tick(run_id)
        deadline = time.monotonic() + 3.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        assert controller.cancel_task(run_id, task.task_id)
        assert controller.store.run_state(run_id) is RunState.CANCELLED
        assert controller.store.committed_output(
            run_id, task.task_id) is None


def test_mpi_request_is_rejected_before_attempt_or_spawn(tmp_path):
    graph = BoundExecutionGraph.bind(
        "uncertified-mpi-is-rejected",
        (TaskTemplate(
            key="no-mpi-in-stage1",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 1},
            resources=ResourceRequest(
                cpu_cores=1, memory_mb=32, mpi_ranks=2, walltime_s=5),
        ),),
    )
    task = graph.tasks[0]
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        assert controller.tick(run_id) is RunState.FAILED
        assert controller.store.task_state(
            run_id, task.task_id) is TaskState.FAILED
        assert _attempt_count(controller, run_id) == 0
        assert not (tmp_path / "staging").exists()


def test_runtime_root_rejects_remote_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "engine.runtime.site.filesystem_type",
        lambda _path: ("nfs", "/remote"),
    )
    with pytest.raises(ValueError, match="node-local POSIX"):
        validate_runtime_root(tmp_path / "remote-runtime")


def test_one_controller_writer_and_guarded_admission(tmp_path):
    """One writer, and concurrency only against a reservation ledger.

    Stage 8 lifted the hard max_inflight=1 rule, but running N attempts while
    knowing nothing about node capacity is precisely the oversubscription the
    reservation ledger exists to prevent, so concurrency requires one.
    """
    first = WorkflowController(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="another WorkflowController"):
            WorkflowController(tmp_path)
        with pytest.raises(ValueError, match="requires a ReservationLedger"):
            WorkflowController(tmp_path / "other", max_inflight=2)
        with pytest.raises(ValueError, match="positive integer"):
            WorkflowController(tmp_path / "other", max_inflight=0)
    finally:
        first.close()


def test_site_snapshot_is_explicitly_private_node():
    site = current_private_site(memory_limit_mb=128)
    assert site.site_id.startswith("private-node:")
    assert site.source == "current-process-envelope"
    assert site.memory_mb == 128
