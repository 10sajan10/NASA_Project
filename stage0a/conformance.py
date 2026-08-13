"""Provider-neutral Stage-0A conformance types and fixture.

The fixture deliberately uses scalar values and sleeps.  It never imports or
executes WRF, MPI, or a site scheduler.
"""
from __future__ import annotations

import dataclasses
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class AttemptState(str, Enum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ResourceRequest:
    cpu_cores: int = 1
    memory_mb: int = 128
    gpus: int = 0
    mpi_ranks: int = 0

    def __post_init__(self) -> None:
        if self.cpu_cores < 1 or self.memory_mb < 1:
            raise ValueError("CPU and memory requests must be positive")
        if self.gpus < 0 or self.mpi_ranks < 0:
            raise ValueError("GPU and MPI requests must be non-negative")


@dataclass(frozen=True)
class AttemptSpec:
    task_id: str
    attempt_id: str
    fencing_token: int
    operation: str
    payload: dict[str, Any]
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.task_id or not self.attempt_id:
            raise ValueError("caller-owned task_id and attempt_id are required")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AttemptSpec":
        raw = dict(value)
        raw["resources"] = ResourceRequest(**raw["resources"])
        return cls(**raw)


@dataclass(frozen=True)
class ExternalHandle:
    provider: str
    external_id: str
    attempt_id: str
    attempt_token: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExternalHandle":
        return cls(**value)


@dataclass(frozen=True)
class ProviderResult:
    state: AttemptState
    output: Any = None
    error: str | None = None


class ExecutionProvider(Protocol):
    name: str

    def submit(self, spec: AttemptSpec) -> ExternalHandle: ...
    def reconcile(self, handle: ExternalHandle) -> AttemptState: ...
    def result(self, handle: ExternalHandle, timeout_s: float) -> ProviderResult: ...
    def cancel(self, handle: ExternalHandle) -> bool: ...
    def restart_view(self) -> "ExecutionProvider": ...
    def close(self) -> None: ...


class CommitDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    INVALID = "INVALID"


class ConformanceArtifactCommitter:
    """Minimal test double for the future authoritative committer.

    It contains no filesystem/catalog implementation.  It proves that every
    provider result must cross a project-owned validation and fencing boundary.
    Stage 1 replaces this class with durable staging and publication.
    """

    def __init__(self) -> None:
        self._leases: dict[str, tuple[str, int]] = {}
        self._committed: dict[str, tuple[str, int, Any]] = {}

    def issue(self, spec: AttemptSpec) -> None:
        current = self._leases.get(spec.task_id)
        if current is not None and spec.fencing_token <= current[1]:
            raise ValueError("new attempt must carry a higher fencing token")
        self._leases[spec.task_id] = (spec.attempt_id, spec.fencing_token)

    def accept(self, spec: AttemptSpec,
               result: ProviderResult) -> CommitDisposition:
        if result.state is not AttemptState.SUCCEEDED or not _valid_output(
                result.output):
            return CommitDisposition.INVALID
        lease = self._leases.get(spec.task_id)
        if lease != (spec.attempt_id, spec.fencing_token):
            return CommitDisposition.STALE
        committed = self._committed.get(spec.task_id)
        if committed is not None:
            if committed[:2] == (spec.attempt_id, spec.fencing_token):
                return CommitDisposition.DUPLICATE
            return CommitDisposition.STALE
        self._committed[spec.task_id] = (
            spec.attempt_id, spec.fencing_token, result.output)
        return CommitDisposition.ACCEPTED

    def value(self, task_id: str) -> Any:
        return self._committed[task_id][2]


def _valid_output(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return isinstance(value, (str, list, dict))


def execute_operation(operation: str, payload: dict[str, Any]) -> Any:
    """Small, picklable workload shared by all candidate providers."""
    if operation == "constant":
        return payload["value"]
    if operation == "add":
        return payload["left"] + payload["right"]
    if operation == "sleep":
        time.sleep(float(payload.get("seconds", 0.05)))
        return payload.get("value", 1)
    if operation == "marker_sleep":
        Path(payload["started_marker"]).touch()
        time.sleep(float(payload.get("seconds", 0.4)))
        Path(payload["completed_marker"]).touch()
        return payload.get("value", 1)
    if operation == "fail":
        raise RuntimeError(str(payload.get("message", "injected failure")))
    raise ValueError(f"unknown conformance operation {operation!r}")


@dataclass
class CheckResult:
    passed: bool
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class ConformanceReport:
    provider: str
    available: bool
    version: str | None
    checks: dict[str, CheckResult]
    timings_s: dict[str, float]
    notes: list[str]

    @property
    def required_pass(self) -> bool:
        required = (
            "caller_identity",
            "resource_metadata",
            "independent_dag",
            "project_owned_commit",
            "late_duplicate_fencing",
            "cancellation",
            "running_work_terminated",
            "restart_honesty",
            "bounded_admission",
            "external_handle_roundtrip",
        )
        return self.available and all(
            self.checks.get(name, CheckResult(False, "missing")).passed
            for name in required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "available": self.available,
            "version": self.version,
            "required_pass": self.required_pass,
            "checks": {k: v.to_dict() for k, v in self.checks.items()},
            "timings_s": self.timings_s,
            "notes": self.notes,
        }


def _wait(provider: ExecutionProvider, handle: ExternalHandle,
          timeout_s: float = 10.0) -> ProviderResult:
    return provider.result(handle, timeout_s)


def run_common_fixture(provider: ExecutionProvider, *,
                       version: str | None = None) -> ConformanceReport:
    checks: dict[str, CheckResult] = {}
    timings: dict[str, float] = {}
    notes: list[str] = []
    committer = ConformanceArtifactCommitter()

    left = AttemptSpec(
        "task-left", "attempt-left-1", 1, "constant", {"value": 19},
        ResourceRequest(cpu_cores=1, memory_mb=64))
    right = AttemptSpec(
        "task-right", "attempt-right-1", 1, "constant", {"value": 23},
        ResourceRequest(cpu_cores=1, memory_mb=96))
    for spec in (left, right):
        committer.issue(spec)

    start = time.monotonic()
    left_h = provider.submit(left)
    right_h = provider.submit(right)
    checks["caller_identity"] = CheckResult(
        left_h.attempt_id == left.attempt_id
        and left_h.attempt_token == f"{left.task_id}:{left.attempt_id}:1",
        f"handle preserved {left_h.attempt_id!r} and stable token")
    checks["resource_metadata"] = CheckResult(
        left_h.metadata.get("resources") == dataclasses.asdict(left.resources)
        and right_h.metadata.get("resources") == dataclasses.asdict(right.resources),
        "caller resource metadata round-tripped in both handles")

    left_r = _wait(provider, left_h)
    right_r = _wait(provider, right_h)
    left_c = committer.accept(left, left_r)
    right_c = committer.accept(right, right_r)
    dependent = AttemptSpec(
        "task-sum", "attempt-sum-1", 1, "add",
        {"left": committer.value("task-left"),
         "right": committer.value("task-right")},
        ResourceRequest(cpu_cores=1, memory_mb=128))
    committer.issue(dependent)
    dep_h = provider.submit(dependent)
    dep_r = _wait(provider, dep_h)
    dep_c = committer.accept(dependent, dep_r)
    timings["three_task_dag"] = time.monotonic() - start
    checks["independent_dag"] = CheckResult(
        left_r.output == 19 and right_r.output == 23 and dep_r.output == 42,
        "two independent producers completed before the dependent consumer")
    checks["project_owned_commit"] = CheckResult(
        (left_c, right_c, dep_c) == (
            CommitDisposition.ACCEPTED,
            CommitDisposition.ACCEPTED,
            CommitDisposition.ACCEPTED),
        "all results crossed the shared project-owned committer")

    stale = AttemptSpec(
        "task-fence", "attempt-fence-1", 1, "constant", {"value": 1})
    fresh = AttemptSpec(
        "task-fence", "attempt-fence-2", 2, "constant", {"value": 2})
    committer.issue(stale)
    committer.issue(fresh)
    stale_d = committer.accept(
        stale, ProviderResult(AttemptState.SUCCEEDED, output=1))
    fresh_d = committer.accept(
        fresh, ProviderResult(AttemptState.SUCCEEDED, output=2))
    duplicate_d = committer.accept(
        fresh, ProviderResult(AttemptState.SUCCEEDED, output=999))
    checks["late_duplicate_fencing"] = CheckResult(
        stale_d is CommitDisposition.STALE
        and fresh_d is CommitDisposition.ACCEPTED
        and duplicate_d is CommitDisposition.DUPLICATE
        and committer.value("task-fence") == 2,
        "stale completion rejected and duplicate could not overwrite value 2")

    marker_id = uuid.uuid4().hex
    started_marker = Path(f"/tmp/stage0a-cancel-{marker_id}.started")
    completed_marker = Path(f"/tmp/stage0a-cancel-{marker_id}.completed")
    slow = AttemptSpec(
        "task-cancel", "attempt-cancel-1", 1, "marker_sleep",
        {
            "seconds": 0.4,
            "value": 7,
            "started_marker": str(started_marker),
            "completed_marker": str(completed_marker),
        }, timeout_s=5.0)
    slow_h = provider.submit(slow)
    marker_deadline = time.monotonic() + 2.0
    while not started_marker.exists() and time.monotonic() < marker_deadline:
        time.sleep(0.01)
    cancelled = provider.cancel(slow_h)
    cancel_state = provider.reconcile(slow_h)
    # A future can report CANCELLED while synchronous worker code keeps
    # running. The completion marker distinguishes request cancellation from
    # actual process/work termination.
    time.sleep(0.5)
    physical_termination = not completed_marker.exists()
    checks["cancellation"] = CheckResult(
        cancelled and cancel_state in {
            AttemptState.CANCELLED, AttemptState.LOST, AttemptState.FAILED},
        f"cancel returned {cancelled}; reconciled as {cancel_state.value}")
    checks["running_work_terminated"] = CheckResult(
        physical_termination,
        "completion marker absent after cancellation" if physical_termination
        else "future was cancelled but synchronous work reached completion")
    started_marker.unlink(missing_ok=True)
    completed_marker.unlink(missing_ok=True)

    restart = AttemptSpec(
        "task-restart", "attempt-restart-1", 1, "sleep",
        {"seconds": 0.25, "value": 11}, timeout_s=3.0)
    restart_h = provider.submit(restart)
    time.sleep(0.03)
    restart_view = provider.restart_view()
    restart_state = restart_view.reconcile(restart_h)
    restart_honest = restart_state in {
        AttemptState.SUBMITTED, AttemptState.RUNNING, AttemptState.SUCCEEDED,
        AttemptState.LOST,
    }
    restart_evidence = f"new controller view reconciled {restart_state.value}"
    if restart_state is not AttemptState.LOST:
        recovered = restart_view.result(restart_h, timeout_s=3.0)
        restart_honest = recovered.state is AttemptState.SUCCEEDED
        restart_evidence += f" then {recovered.state.value}"
    else:
        notes.append(
            "active work cannot be reattached; the provider honestly returns "
            "LOST and Stage 1 must fence/retry only safe work")
    checks["restart_honesty"] = CheckResult(
        restart_honest, restart_evidence)
    if restart_view is not provider:
        restart_view.close()

    max_inflight = 2
    specs = [AttemptSpec(
        f"task-window-{i}", f"attempt-window-{i}-1", 1, "sleep",
        {"seconds": 0.015, "value": i}) for i in range(8)]
    pending: list[tuple[AttemptSpec, ExternalHandle]] = []
    cursor = 0
    peak = 0
    values: list[int] = []
    while cursor < len(specs) or pending:
        while cursor < len(specs) and len(pending) < max_inflight:
            spec = specs[cursor]
            pending.append((spec, provider.submit(spec)))
            cursor += 1
            peak = max(peak, len(pending))
        spec, handle = pending.pop(0)
        output = _wait(provider, handle).output
        values.append(output)
    checks["bounded_admission"] = CheckResult(
        peak <= max_inflight and sorted(values) == list(range(8)),
        f"controller admitted peak={peak} for eight logical partitions")

    fake = ExternalHandle(
        provider="fake-batch",
        external_id="job-123",
        attempt_id="attempt-fake-1",
        attempt_token="task-fake:attempt-fake-1:1",
        metadata={"scheduler": "not-contacted", "mpi_ranks": 4})
    roundtrip = ExternalHandle.from_dict(json.loads(json.dumps(fake.to_dict())))
    checks["external_handle_roundtrip"] = CheckResult(
        roundtrip == fake,
        "opaque fake batch/MPI handle survived persistence without invocation")
    checks["mpi_execution"] = CheckResult(
        False,
        "not executed: this private development node has no certified MPI/SLURM profile")

    return ConformanceReport(
        provider=provider.name,
        available=True,
        version=version,
        checks=checks,
        timings_s=timings,
        notes=notes,
    )
