"""Focused tests for the Stage-1 supervised local subprocess boundary.

Every workload is a tiny closed-registry synthetic operation.  These tests do
not import or invoke WRF, MPI, SLURM, or an arbitrary external command.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path

import pytest

from engine.runtime.artifacts import AFTER_RESULT_PARSED
from engine.runtime.controller import WorkflowController
from engine.runtime.identity import strict_canonical_json
from engine.runtime.operations import operation_component
from engine.runtime.provider import (
    LocalSubprocessProvider,
    SubmissionOutcomeUnknown,
)
from engine.runtime.site import current_private_site
from engine.runtime.types import (
    AttemptInputReceipt,
    AttemptSpec,
    AttemptState,
    BoundExecutionGraph,
    InputBinding,
    ResourceRequest,
    RunState,
    TaskState,
    TaskTemplate,
    attempt_id,
    deployment_id,
)


def _provider(root: Path) -> LocalSubprocessProvider:
    site = current_private_site(memory_limit_mb=1024)
    return LocalSubprocessProvider(
        root, site=site, cancellation_grace_s=0.2)


def _spec(provider: LocalSubprocessProvider,
          graph: BoundExecutionGraph, task_key: str, *,
          run_id: str,
          input_artifacts: dict[str, AttemptInputReceipt] | None = None,
          stage_dir: Path | None = None) -> AttemptSpec:
    task = graph.task_by_key(task_key)
    binding = deployment_id(graph.plan_id, provider.site, provider.name)
    aid = attempt_id(run_id, task.task_id, binding, 1, 1)
    stage = stage_dir or provider.root / "staging" / aid
    return AttemptSpec(
        run_id=run_id,
        deployment_id=binding,
        task=task,
        attempt_id=aid,
        attempt_number=1,
        fencing_token=1,
        provider=provider.name,
        input_artifacts=dict(input_artifacts or {}),
        stage_dir=str(stage),
        created_at=time.time(),
    )


def _wait(provider: LocalSubprocessProvider, handle, *,
          timeout_s: float = 5.0):
    deadline = time.monotonic() + timeout_s
    observation = provider.reconcile(handle)
    while (observation.state is AttemptState.RUNNING
           and time.monotonic() < deadline):
        time.sleep(0.01)
        observation = provider.reconcile(handle)
    if observation.state is AttemptState.RUNNING:
        provider.cancel(handle)
        pytest.fail("local subprocess did not reach a terminal marker")
    return observation


def test_local_worker_publishes_complete_result_marker_last(tmp_path):
    provider = _provider(tmp_path)
    graph = BoundExecutionGraph.bind(
        "provider-smoke",
        (TaskTemplate(
            key="source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 42},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    spec = _spec(provider, graph, "source", run_id="provider-smoke")

    handle = provider.submit(spec)
    observation = _wait(provider, handle)

    assert observation.state is AttemptState.RESULT_READY
    result_path = Path(observation.result_manifest_path)
    result = json.loads(result_path.read_text())
    # Peak RSS is optional telemetry beside the scientific result; it never
    # participates in artifact identity or commit.
    peak = result.pop("peak_memory_kb", None)
    assert result == {
        "schema": "stage1-attempt-result-v1",
        "attempt_id": spec.attempt_id,
        "attempt_token": spec.attempt_token,
        "outputs": {"result": {"path": "outputs/result/payload.json"}},
    }
    assert isinstance(peak, int) and peak > 0
    assert json.loads(
        (Path(spec.stage_dir) / "outputs/result/payload.json").read_text()) == 42
    assert not (Path(spec.stage_dir) / "error.json").exists()


def test_reconstructed_provider_recovers_running_attempt_by_token(tmp_path):
    marker = tmp_path / "worker-started"
    first = _provider(tmp_path)
    graph = BoundExecutionGraph.bind(
        "provider-restart",
        (TaskTemplate(
            key="slow",
            component=operation_component("synthetic.sleep.v1"),
            parameters={
                "seconds": 0.4,
                "value": 9,
                "started_marker": str(marker),
            },
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    spec = _spec(first, graph, "slow", run_id="provider-restart")
    original = first.submit(spec)
    deadline = time.monotonic() + 3.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    first.close()  # losing a controller view must not kill the attempt

    reconstructed = _provider(tmp_path)
    observation = reconstructed.reconcile_token(spec)
    assert observation.recovered_handle is not None
    recovered = observation.recovered_handle
    assert recovered.attempt_id == original.attempt_id
    assert recovered.attempt_token == original.attempt_token
    if observation.state is AttemptState.RUNNING:
        observation = _wait(reconstructed, recovered)

    assert observation.state is AttemptState.RESULT_READY
    assert json.loads(
        (Path(spec.stage_dir) / "outputs/result/payload.json").read_text()) == 9


def test_cancel_terminates_the_recorded_process_group(tmp_path):
    marker = tmp_path / "cancel-started"
    provider = _provider(tmp_path)
    graph = BoundExecutionGraph.bind(
        "provider-cancel",
        (TaskTemplate(
            key="slow",
            component=operation_component("synthetic.sleep.v1"),
            parameters={
                "seconds": 5.0,
                "value": 7,
                "started_marker": str(marker),
            },
            resources=ResourceRequest(memory_mb=32, walltime_s=10),
        ),),
    )
    spec = _spec(provider, graph, "slow", run_id="provider-cancel")
    handle = provider.submit(spec)
    deadline = time.monotonic() + 3.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()

    assert provider.cancel(handle)
    observation = provider.reconcile(handle)

    assert observation.state is AttemptState.CANCELLED
    assert not (Path(spec.stage_dir) / "result.json").exists()
    assert (Path(spec.stage_dir) / "supervisor/cancelled.json").is_file()


def test_tampered_component_binding_is_rejected_before_spawn(tmp_path):
    provider = _provider(tmp_path)
    tampered = dataclasses.replace(
        operation_component("synthetic.constant.v1"),
        implementation_digest="0" * 64,
    )
    graph = BoundExecutionGraph.bind(
        "provider-closed-registry",
        (TaskTemplate(
            key="source",
            component=tampered,
            parameters={"value": 1},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    spec = _spec(provider, graph, "source", run_id="closed-registry")

    with pytest.raises(ValueError, match="closed-registry binding"):
        provider.submit(spec)

    assert not Path(spec.stage_dir).exists()


def test_stage_path_outside_runtime_root_is_rejected(tmp_path):
    provider = _provider(tmp_path)
    graph = BoundExecutionGraph.bind(
        "provider-path-confinement",
        (TaskTemplate(
            key="source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 1},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside-stage"
    spec = _spec(
        provider, graph, "source", run_id="path-confinement",
        stage_dir=outside,
    )

    with pytest.raises(ValueError, match="escapes the runtime root"):
        provider.submit(spec)

    assert not outside.exists()


def test_worker_rejects_input_object_escape(tmp_path):
    provider = _provider(tmp_path)
    producer = TaskTemplate(
        key="upstream",
        component=operation_component("synthetic.constant.v1"),
        parameters={"value": 3},
    )
    consumer = TaskTemplate(
        key="consumer",
        component=operation_component("synthetic.identity.v1"),
        inputs=(InputBinding("value", "upstream", "result"),),
        resources=ResourceRequest(memory_mb=32, walltime_s=5),
    )
    graph = BoundExecutionGraph.bind("input-confinement", (producer, consumer))
    manifest_path = tmp_path / "manifests" / "input.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(strict_canonical_json({
        "schema": "stage1-artifact-manifest-v1",
        "artifact_id": "a" * 64,
        "recipe_id": "b" * 64,
        "media_type": "application/json",
        "content_sha256": hashlib.sha256(b"3").hexdigest(),
        "size_bytes": 1,
        "object_path": "../outside-runtime.json",
    }))
    spec = _spec(
        provider, graph, "consumer", run_id="input-confinement",
        input_artifacts={"value": AttemptInputReceipt(
            artifact_id="a" * 64,
            recipe_id="b" * 64,
            content_sha256=hashlib.sha256(b"3").hexdigest(),
            size_bytes=1,
            manifest_path=str(manifest_path.resolve()),
        )},
    )

    handle = provider.submit(spec)
    observation = _wait(provider, handle)

    assert observation.state is AttemptState.FAILED
    assert "escapes the runtime root" in observation.error
    assert not (Path(spec.stage_dir) / "result.json").exists()


def test_worker_rejects_manifest_substitution_against_attempt_receipt(tmp_path):
    provider = _provider(tmp_path)
    producer = TaskTemplate(
        key="upstream",
        component=operation_component("synthetic.constant.v1"),
        parameters={"value": 3},
    )
    consumer = TaskTemplate(
        key="consumer",
        component=operation_component("synthetic.identity.v1"),
        inputs=(InputBinding("value", "upstream", "result"),),
        resources=ResourceRequest(memory_mb=32, walltime_s=5),
    )
    graph = BoundExecutionGraph.bind(
        "input-receipt-substitution", (producer, consumer))
    objects = tmp_path / "objects"
    manifests = tmp_path / "manifests"
    objects.mkdir()
    manifests.mkdir()
    first_payload, second_payload = b"3", b"4"
    (objects / "first.json").write_bytes(first_payload)
    (objects / "second.json").write_bytes(second_payload)
    manifest_path = manifests / "input.json"
    first_digest = hashlib.sha256(first_payload).hexdigest()
    first_artifact = "a" * 64
    recipe_id = "b" * 64
    manifest_path.write_text(strict_canonical_json({
        "schema": "stage1-artifact-manifest-v1",
        "artifact_id": first_artifact,
        "recipe_id": recipe_id,
        "media_type": "application/json",
        "content_sha256": first_digest,
        "size_bytes": 1,
        "object_path": "objects/first.json",
    }))
    spec = _spec(
        provider, graph, "consumer", run_id="input-substitution",
        input_artifacts={"value": AttemptInputReceipt(
            artifact_id=first_artifact,
            recipe_id=recipe_id,
            content_sha256=first_digest,
            size_bytes=1,
            manifest_path=str(manifest_path.resolve()),
        )},
    )

    # Replace the locator's contents with a different valid-looking manifest.
    manifest_path.write_text(strict_canonical_json({
        "schema": "stage1-artifact-manifest-v1",
        "artifact_id": "c" * 64,
        "recipe_id": recipe_id,
        "media_type": "application/json",
        "content_sha256": hashlib.sha256(second_payload).hexdigest(),
        "size_bytes": 1,
        "object_path": "objects/second.json",
    }))
    observation = _wait(provider, provider.submit(spec))
    assert observation.state is AttemptState.FAILED
    assert "exact attempt receipt" in observation.error
    assert not (Path(spec.stage_dir) / "result.json").exists()


def test_cancel_supersedes_result_that_already_reached_validation(tmp_path):
    def crash_before_validation(_boundary: str) -> None:
        if _boundary == AFTER_RESULT_PARSED:
            raise RuntimeError("pause before artifact validation")

    graph = BoundExecutionGraph.bind(
        "cancel-result-ready",
        (TaskTemplate(
            key="source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 42},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    task = graph.tasks[0]
    with WorkflowController(
            tmp_path, artifact_failpoint=crash_before_validation) as controller:
        run_id = controller.create_run(graph)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                controller.tick(run_id)
            except RuntimeError as exc:
                assert "pause before artifact validation" in str(exc)
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        with controller.store.connect() as con:
            attempt_id = con.execute(
                "SELECT attempt_id FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0]
        assert controller.store.attempt_record(
            attempt_id).state is AttemptState.RESULT_READY
        assert controller.store.task_state(
            run_id, task.task_id) is TaskState.VALIDATING

        assert controller.cancel_task(run_id, task.task_id)
        assert controller.store.run_state(run_id) is RunState.CANCELLED
        assert controller.store.attempt_record(
            attempt_id).state is AttemptState.SUPERSEDED
        assert controller.store.active_attempt_count(run_id) == 0
        assert controller.store.committed_output(
            run_id, task.task_id) is None


def test_cancel_terminalizes_unknown_submission_without_receipt(
        tmp_path, monkeypatch):
    provider = _provider(tmp_path)

    def fail_before_provider_receipt(_spec: AttemptSpec):
        raise SubmissionOutcomeUnknown("injected pre-receipt uncertainty")

    monkeypatch.setattr(provider, "submit", fail_before_provider_receipt)
    graph = BoundExecutionGraph.bind(
        "cancel-pre-receipt-unknown",
        (TaskTemplate(
            key="source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": 1},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )
    task = graph.tasks[0]
    with WorkflowController(
            tmp_path, site=provider.site, provider=provider) as controller:
        run_id = controller.create_run(graph)
        controller.tick(run_id)
        with controller.store.connect() as con:
            attempt_id = con.execute(
                "SELECT attempt_id FROM attempts WHERE run_id=?", (run_id,),
            ).fetchone()[0]
        record = controller.store.attempt_record(attempt_id)
        assert record.state is AttemptState.SUBMISSION_UNKNOWN
        assert not (Path(record.spec.stage_dir)
                    / "supervisor/attempt.json").exists()

        assert controller.cancel_task(run_id, task.task_id)
        assert controller.store.run_state(run_id) is RunState.CANCELLED
        assert controller.store.attempt_record(
            attempt_id).state is AttemptState.LOST
        assert controller.store.active_attempt_count(run_id) == 0
