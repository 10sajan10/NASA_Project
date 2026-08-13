"""Tests for the disposable Stage-0A provider comparison."""
from __future__ import annotations

from pathlib import Path

from stage0a.conformance import (
    AttemptSpec,
    AttemptState,
    CommitDisposition,
    ConformanceArtifactCommitter,
    ExternalHandle,
    ProviderResult,
    ResourceRequest,
    run_common_fixture,
)
from stage0a.providers import ThinLocalSubprocessProvider, candidate_availability


def test_attempt_and_external_handle_round_trip():
    spec = AttemptSpec(
        task_id="task-1",
        attempt_id="attempt-1",
        fencing_token=3,
        operation="constant",
        payload={"value": 4},
        resources=ResourceRequest(cpu_cores=2, memory_mb=512, gpus=1),
    )
    assert AttemptSpec.from_dict(spec.to_dict()) == spec
    handle = ExternalHandle(
        provider="fake", external_id="job-1",
        attempt_id=spec.attempt_id,
        attempt_token="task-1:attempt-1:3",
        metadata={"site": "fake"},
    )
    assert ExternalHandle.from_dict(handle.to_dict()) == handle


def test_committer_rejects_invalid_stale_and_conflicting_results():
    committer = ConformanceArtifactCommitter()
    old = AttemptSpec("task", "attempt-1", 1, "constant", {"value": 1})
    current = AttemptSpec("task", "attempt-2", 2, "constant", {"value": 2})
    committer.issue(old)
    committer.issue(current)

    assert committer.accept(
        old, ProviderResult(AttemptState.SUCCEEDED, output=1),
    ) is CommitDisposition.STALE
    assert committer.accept(
        current, ProviderResult(AttemptState.FAILED, error="bad"),
    ) is CommitDisposition.INVALID
    assert committer.accept(
        current, ProviderResult(AttemptState.SUCCEEDED, output=2),
    ) is CommitDisposition.ACCEPTED
    assert committer.accept(
        current, ProviderResult(AttemptState.SUCCEEDED, output=999),
    ) is CommitDisposition.DUPLICATE
    assert committer.value("task") == 2


def test_thin_local_subprocess_passes_common_fixture(tmp_path):
    # pytest's configured tmp_path is under /tmp on the development node.
    assert str(tmp_path).startswith("/tmp/")
    provider = ThinLocalSubprocessProvider(tmp_path / "provider", max_workers=2)
    try:
        report = run_common_fixture(provider, version="test")
    finally:
        provider.close()

    assert report.required_pass
    assert not report.checks["mpi_execution"].passed
    assert "not executed" in report.checks["mpi_execution"].evidence


def test_framework_availability_report_is_explicit():
    availability = candidate_availability()
    assert set(availability) == {
        "thin-local-subprocess",
        "dask-distributed-local",
        "parsl-local",
    }
    assert availability["thin-local-subprocess"]["available"] is True
