"""Stage-9A conditional SLURM provider: nonblocking submit, safe recovery.

The whole difficulty of a queued provider is the window between `sbatch`
returning a job ID and that ID reaching durable storage. A controller that
crashes inside that window and then resubmits has silently run the science
twice. So identity does not depend on our own bookkeeping surviving: every job
carries its **attempt token in its SLURM job name**, and recovery asks the
cluster "is there already a job for this attempt?" before it will submit.

When the cluster cannot answer that question -- `squeue` and `sacct` both
failing, or accounting not yet reflecting a very recent submission -- the
answer is `SUBMISSION_UNKNOWN`, never a fresh `sbatch`. An unknown submission
is a human's problem; a duplicate one is a corrupted experiment.

Scientific identity is deliberately independent of where SLURM chose to run the
job: node lists appear in metadata for operators, never in an attempt token or
artifact identity.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .identity import (
    require_object_fields,
    strict_canonical_json,
    strict_copy,
    strict_hash,
    strict_json_loads,
)
from .operations import operation_component
from .provider import _valid_result_manifest
from .site import current_private_site, preflight_request
from .types import (
    AttemptSpec,
    AttemptState,
    ExternalHandle,
    ProviderObservation,
    SiteSnapshot,
)

PROVIDER_NAME = "stage9a-slurm"

# SLURM job names are the recovery index.  A stable, greppable prefix keeps
# `squeue --name` and `sacct --name` lookups exact rather than fuzzy.
JOB_NAME_PREFIX = "nasa-attempt-"
_JOB_ID = re.compile(r"^[1-9][0-9]*$")
_ATTEMPT_FILE = "attempt.json"
_JOB_RECEIPT = "slurm_job.json"
_RESULT_FILE = "result.json"
_ERROR_FILE = "error.json"

# States SLURM reports that mean the job is gone and did not succeed.
_FAILED_STATES = frozenset({
    "FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL",
    "DEADLINE", "PREEMPTED", "REVOKED", "SPECIAL_EXIT",
})
_CANCELLED_STATES = frozenset({"CANCELLED"})
_ACTIVE_STATES = frozenset({
    "PENDING", "CONFIGURING", "RUNNING", "COMPLETING", "RESIZING",
    "SUSPENDED", "REQUEUED", "REQUEUE_FED", "REQUEUE_HOLD", "SIGNALING",
    "STAGE_OUT", "STOPPED",
})
_COMPLETED_STATES = frozenset({"COMPLETED"})


class SlurmUnavailable(RuntimeError):
    """The SLURM client tools are absent or the controller is unreachable."""


class SchedulerVisibilityStatus(str, Enum):
    """What one independent scheduler view can prove about an attempt."""

    FOUND = "FOUND"
    CONFIRMED_ABSENT = "CONFIRMED_ABSENT"
    UNAVAILABLE_OR_INCOMPLETE = "UNAVAILABLE_OR_INCOMPLETE"


@dataclass(frozen=True)
class SchedulerVisibility:
    source: str
    status: SchedulerVisibilityStatus
    job_id: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.source not in {"squeue", "sacct"}:
            raise ValueError("scheduler visibility source must be squeue or sacct")
        if not isinstance(self.status, SchedulerVisibilityStatus):
            raise TypeError("scheduler visibility status must be typed")
        if self.status is SchedulerVisibilityStatus.FOUND:
            if not isinstance(self.job_id, str) or not self.job_id.strip():
                raise ValueError("FOUND scheduler visibility needs a job ID")
        elif self.job_id is not None:
            raise ValueError("only FOUND scheduler visibility may name a job")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


CommandRunner = Callable[[Sequence[str]], CommandResult]


def real_command_runner(timeout_s: float = 60.0) -> CommandRunner:
    """Run SLURM client commands as subprocesses."""
    def run(argv: Sequence[str]) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout_s,
                check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(returncode=-1, stdout="", stderr=str(exc))
        return CommandResult(completed.returncode, completed.stdout,
                             completed.stderr)
    return run


def slurm_is_available() -> bool:
    """Whether the client tools are installed *and executable here*.

    Merely finding binaries is not enough on development nodes whose local
    identity cannot load the site's SLURM configuration.  ``--help`` is a
    read-only client probe; it queues no work.
    """
    for name in ("sbatch", "squeue", "sacct", "scancel"):
        if shutil.which(name) is None:
            return False
        try:
            probe = subprocess.run(
                [name, "--help"], capture_output=True, text=True,
                timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if probe.returncode != 0 or not probe.stdout.strip():
            return False
    return True


def job_name_for(spec: AttemptSpec) -> str:
    """The recovery key. Derived only from the attempt token.

    Not from the node, the partition, or anything SLURM chooses, so the same
    attempt is findable regardless of where it landed.
    """
    return f"{JOB_NAME_PREFIX}{spec.attempt_token}"


@dataclass(frozen=True)
class SlurmSubmitOptions:
    """Queue-facing knobs. None of these touch scientific identity."""

    partition: str | None = None
    account: str | None = None
    time_limit: str = "00:10:00"
    cpus_per_task: int = 1
    memory_mb: int = 512
    nodes: int = 1
    ntasks: int = 1

    def __post_init__(self) -> None:
        for name in ("cpus_per_task", "memory_mb", "nodes", "ntasks"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.time_limit, str) or not self.time_limit.strip():
            raise ValueError("a SLURM time limit is required")
        if not re.fullmatch(r"(?:[0-9]+-)?[0-9]{1,2}:[0-9]{2}:[0-9]{2}",
                            self.time_limit):
            raise ValueError(
                "SLURM time_limit must use [days-]HH:MM:SS")
        for name in ("partition", "account"):
            value = getattr(self, name)
            if value is not None and (
                    not isinstance(value, str) or not value.strip()
                    or any(character in value for character in "\r\n\0")):
                raise ValueError(f"SLURM {name} must be safe non-empty text")
        if self.nodes != 1 or self.ntasks != 1:
            raise ValueError(
                "the closed Stage-9A worker supports one node and one task; "
                "MPI/multi-node launch is not implemented")
        if self.time_limit_seconds < 1:
            raise ValueError("SLURM time_limit must be positive")

    @property
    def time_limit_seconds(self) -> int:
        day_part, clock = (self.time_limit.split("-", 1)
                           if "-" in self.time_limit
                           else ("0", self.time_limit))
        hours, minutes, seconds = (int(item) for item in clock.split(":"))
        if minutes >= 60 or seconds >= 60:
            raise ValueError("SLURM time_limit minutes/seconds must be below 60")
        return int(day_part) * 86400 + hours * 3600 + minutes * 60 + seconds

    def sbatch_arguments(self, spec: AttemptSpec, log_path: Path
                         ) -> list[str]:
        argv = [
            "sbatch",
            "--parsable",
            f"--job-name={job_name_for(spec)}",
            f"--time={self.time_limit}",
            f"--nodes={self.nodes}",
            f"--ntasks={self.ntasks}",
            f"--cpus-per-task={self.cpus_per_task}",
            f"--mem={self.memory_mb}M",
            f"--output={log_path}",
            f"--error={log_path}",
        ]
        if self.partition:
            argv.append(f"--partition={self.partition}")
        if self.account:
            argv.append(f"--account={self.account}")
        return argv


class SlurmProvider:
    """A nonblocking SLURM execution provider with token-based recovery."""

    name = PROVIDER_NAME

    def __init__(self, root: Path | str, *,
                 options: SlurmSubmitOptions = SlurmSubmitOptions(),
                 runner: CommandRunner | None = None,
                 require_tools: bool = True,
                 site: SiteSnapshot | None = None,
                 python_executable: Path | str | None = None) -> None:
        raw_root = Path(root)
        if not raw_root.is_absolute():
            raise ValueError("SLURM provider root must be absolute")
        self.root = raw_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("SLURM provider root must be a directory")
        if not isinstance(options, SlurmSubmitOptions):
            raise TypeError("options must be SlurmSubmitOptions")
        self.options = options
        self.site = site or current_private_site()
        if not isinstance(self.site, SiteSnapshot):
            raise TypeError("SLURM provider site must be SiteSnapshot")
        executable = Path(python_executable or sys.executable)
        if not executable.is_absolute():
            raise ValueError("SLURM worker Python executable must be absolute")
        if not executable.exists() or not os.access(executable, os.X_OK):
            raise ValueError(
                "SLURM worker Python executable must exist and be executable")
        # Preserve a virtual-environment launcher path. Resolving that symlink
        # to /usr/bin/python discards the environment's installed packages.
        self.python_executable = str(executable)
        self._run = runner or real_command_runner()
        self._closed = False
        if require_tools and runner is None and not slurm_is_available():
            raise SlurmUnavailable(
                "sbatch/squeue/sacct/scancel are not on PATH; this provider "
                "requires a real SLURM installation")

    def execution_sites(self) -> tuple[SiteSnapshot, ...]:
        """The exact deployment envelope this controller bound to the queue."""
        return (self.site,)

    def close(self) -> None:
        """Drop this controller view without cancelling durable queue jobs."""
        self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SLURM provider is closed")

    def _validate_spec(self, spec: AttemptSpec) -> Path:
        if not isinstance(spec, AttemptSpec):
            raise TypeError("SLURM provider requires an AttemptSpec")
        if spec.provider != self.name:
            raise ValueError(
                f"attempt requests provider {spec.provider!r}, "
                f"expected {self.name!r}")
        if AttemptSpec.from_dict(spec.to_dict()) != spec:
            raise ValueError("attempt specification identity does not verify")
        if not re.fullmatch(r"[0-9a-f]{64}", spec.attempt_token):
            raise ValueError("attempt token is not a lowercase SHA-256 digest")
        if operation_component(
                spec.task.component.operation_key) != spec.task.component:
            raise ValueError(
                "attempt component is not an exact closed-registry binding")
        preflight_request(spec.task.resources, self.site)
        request = spec.task.resources
        if request.gpus != 0:
            raise ValueError(
                "Stage-9A does not pin GPU device identities; GPU jobs are refused")
        if request.mpi_ranks != 0:
            raise ValueError("Stage-9A does not implement MPI launch")
        if request.cpu_cores > self.options.cpus_per_task:
            raise ValueError("SLURM CPU request is smaller than the task envelope")
        if request.memory_mb > self.options.memory_mb:
            raise ValueError("SLURM memory request is smaller than the task envelope")
        if request.walltime_s > self.options.time_limit_seconds:
            raise ValueError(
                "SLURM time limit is smaller than the task walltime envelope")
        return self._stage_dir(spec.stage_dir)

    def _stage_dir(self, raw: str | Path) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("attempt stage_dir must be absolute")
        if path.exists() and path.is_symlink():
            raise ValueError("attempt stage_dir cannot be a symlink")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "attempt stage_dir escapes the SLURM provider root") from exc
        return resolved

    def _prepare_attempt(self, spec: AttemptSpec) -> tuple[Path, str]:
        """Publish the immutable worker spec and deterministic queue script."""
        stage = self._validate_spec(spec)
        supervisor = stage / "supervisor"
        supervisor.mkdir(parents=True, exist_ok=True)
        if supervisor.is_symlink():
            raise ValueError("attempt supervisor directory cannot be a symlink")
        _write_once(
            supervisor / _ATTEMPT_FILE,
            (strict_canonical_json(spec.to_dict()) + "\n").encode("ascii"),
            mode=0o600,
        )
        script = self._job_script(spec, stage)
        script_path = supervisor / "job.sh"
        _write_once(script_path, script, mode=0o700)
        if script_path.is_symlink() or not script_path.is_file():
            raise RuntimeError("SLURM attempt script is not a regular file")
        script_digest = hashlib.sha256(script).hexdigest()
        if hashlib.sha256(script_path.read_bytes()).hexdigest() != script_digest:
            raise RuntimeError("SLURM attempt script content identity changed")
        return stage, script_digest

    def _job_script(self, spec: AttemptSpec, stage: Path) -> bytes:
        threads = str(max(1, spec.task.resources.cpu_cores))
        project_root = Path(__file__).resolve().parents[2]
        values = {
            "python": self.python_executable,
            "project_root": str(project_root),
            "runtime_root": str(self.root),
            "stage_dir": str(stage),
            "attempt_token": spec.attempt_token,
            "provider": self.name,
            "threads": threads,
        }
        quoted = {key: shlex.quote(value) for key, value in values.items()}
        return (
            "#!/bin/sh\n"
            "set -eu\n"
            f"export OMP_NUM_THREADS={quoted['threads']}\n"
            f"export OPENBLAS_NUM_THREADS={quoted['threads']}\n"
            f"export MKL_NUM_THREADS={quoted['threads']}\n"
            f"export NUMEXPR_NUM_THREADS={quoted['threads']}\n"
            f"cd {quoted['project_root']}\n"
            f"exec {quoted['python']} -m engine.runtime.worker "
            f"--runtime-root {quoted['runtime_root']} "
            f"--stage-dir {quoted['stage_dir']} "
            f"--attempt-token {quoted['attempt_token']} "
            f"--provider-name {quoted['provider']}\n"
        ).encode("utf-8")

    # -- lookup ----------------------------------------------------------

    def find_existing_job(self, spec: AttemptSpec) -> str | None:
        """Ask the cluster whether this attempt already has a job.

        Checks the live queue first, then accounting, because a job that
        finished between a crash and this call is no longer in `squeue` but is
        in `sacct`.  Raises rather than guessing when neither can answer.
        """
        self._require_open()
        self._validate_spec(spec)
        queue_view, accounting_view = self.lookup_existing_job(spec)
        found_ids = {
            view.job_id for view in (queue_view, accounting_view)
            if view.status is SchedulerVisibilityStatus.FOUND
        }
        if len(found_ids) > 1:
            raise SlurmUnavailable(
                "squeue and sacct disagree about the job for one stable "
                f"attempt token: {tuple(sorted(found_ids))}")
        if found_ids:
            return next(iter(found_ids))
        unavailable = tuple(
            view for view in (queue_view, accounting_view)
            if view.status is SchedulerVisibilityStatus.UNAVAILABLE_OR_INCOMPLETE)
        if unavailable:
            detail = " / ".join(
                f"{view.source}: {view.detail or view.status.value}"
                for view in unavailable)
            raise SlurmUnavailable(
                "scheduler absence is not proven independently by both "
                f"squeue and sacct for attempt {spec.attempt_id}: {detail}")
        return None

    def lookup_existing_job(
            self, spec: AttemptSpec,
    ) -> tuple[SchedulerVisibility, SchedulerVisibility]:
        """Query queue and accounting independently, never merging absence.

        A live-queue miss cannot compensate for unavailable accounting, and
        an accounting miss cannot compensate for an unavailable live queue.
        Only two explicit ``CONFIRMED_ABSENT`` observations authorize a first
        submission.  A positive observation from either source is sufficient
        to recover the existing job.
        """
        self._require_open()
        self._validate_spec(spec)
        name = job_name_for(spec)
        queued = self._run(
            ["squeue", "--noheader", "--name", name, "--format=%i",
             "--states=all"])
        if queued.ok:
            job_ids = tuple(sorted({
                _job_id(line.strip().split(".")[0])
                for line in queued.stdout.strip().splitlines()
                if line.strip()
            }))
            if len(job_ids) > 1:
                raise SlurmUnavailable(
                    "squeue returned more than one job for one stable "
                    f"attempt token: {job_ids}")
            if job_ids:
                queue_view = SchedulerVisibility(
                    "squeue", SchedulerVisibilityStatus.FOUND,
                    job_ids[0])
            else:
                queue_view = SchedulerVisibility(
                    "squeue", SchedulerVisibilityStatus.CONFIRMED_ABSENT)
        else:
            queue_view = SchedulerVisibility(
                "squeue",
                SchedulerVisibilityStatus.UNAVAILABLE_OR_INCOMPLETE,
                detail=queued.stderr.strip() or "squeue query failed")
        accounted = self._run(
            ["sacct", "--noheader", "--parsable2", "--name", name,
             "--format=JobID,State"])
        accounted_ids: set[str] = set()
        if accounted.ok:
            for line in accounted.stdout.strip().splitlines():
                fields = line.split("|")
                if fields and fields[0] and "." not in fields[0]:
                    accounted_ids.add(_job_id(fields[0].strip()))
            if len(accounted_ids) > 1:
                raise SlurmUnavailable(
                    "sacct returned more than one job for one stable "
                    f"attempt token: {tuple(sorted(accounted_ids))}")
            accounted_id = (next(iter(accounted_ids))
                              if accounted_ids else None)
            accounting_view = SchedulerVisibility(
                "sacct",
                (SchedulerVisibilityStatus.FOUND if accounted_id
                 else SchedulerVisibilityStatus.CONFIRMED_ABSENT),
                accounted_id)
        else:
            accounting_view = SchedulerVisibility(
                "sacct",
                SchedulerVisibilityStatus.UNAVAILABLE_OR_INCOMPLETE,
                detail=accounted.stderr.strip() or "sacct query failed")
        return queue_view, accounting_view

    # -- submit ----------------------------------------------------------

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        """Submit, but only after confirming no job exists for this attempt.

        The pre-check is what makes a crash between `sbatch` and handle
        persistence survivable: the retry finds the existing job by token
        instead of starting a second one.
        """
        self._require_open()
        stage, script_digest = self._prepare_attempt(spec)
        existing = self.find_existing_job(spec)
        if existing is not None:
            return self._handle(
                spec, existing, recovered=True,
                script_digest=script_digest)

        # This durable, exclusive marker is written only after both scheduler
        # views proved absence and immediately before sbatch.  A later process
        # finding it may recover a visible job, but may never infer from a
        # temporary dual miss that the original call did not land.
        if not self._create_submission_intent(spec, script_digest):
            raise SlurmUnavailable(
                "a prior durable submission intent is unresolved; absence "
                "from current scheduler views cannot authorize another sbatch")

        log_path = stage / "supervisor" / "slurm.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = self.options.sbatch_arguments(spec, log_path)
        argv.append(str(stage / "supervisor" / "job.sh"))
        result = self._run(argv)
        if not result.ok:
            # Submission may still have landed; re-ask before concluding.
            recheck = self.find_existing_job(spec)
            if recheck is not None:
                return self._handle(
                    spec, recheck, recovered=True,
                    script_digest=script_digest)
            raise SlurmUnavailable(
                f"sbatch failed for attempt {spec.attempt_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}")
        try:
            job_id = _job_id(result.stdout.strip().split(";")[0].strip())
        except ValueError as exc:
            # An unparsable client response does not prove rejection. Recover
            # by the stable job name before classifying the outcome unknown.
            recheck = self.find_existing_job(spec)
            if recheck is not None:
                return self._handle(
                    spec, recheck, recovered=True,
                    script_digest=script_digest)
            raise SlurmUnavailable(
                "sbatch returned no valid base job ID; submission outcome "
                "cannot be trusted") from exc
        return self._handle(
            spec, job_id, recovered=False, script_digest=script_digest)

    # -- reconcile -------------------------------------------------------

    def reconcile(
            self, value: ExternalHandle | AttemptSpec,
            spec: AttemptSpec | None = None,
    ) -> ProviderObservation:
        """Observe a durable handle or recover one stable attempt token."""
        self._require_open()
        if isinstance(value, AttemptSpec):
            if spec is not None:
                raise TypeError(
                    "a token reconciliation accepts one AttemptSpec only")
            return self.reconcile_token(value)
        if not isinstance(value, ExternalHandle):
            raise TypeError("reconcile requires ExternalHandle or AttemptSpec")
        recorded = self._spec_for_handle(value)
        if spec is not None:
            self._validate_spec(spec)
            if spec != recorded:
                raise ValueError("supplied spec disagrees with handle receipt")
        state = self._batch_states((value.external_id,)).get(value.external_id)
        return self._observation(value, recorded, state)

    def reconcile_token(self, spec: AttemptSpec) -> ProviderObservation:
        """Recover SUBMITTING/SUBMISSION_UNKNOWN by stable queue job name."""
        self._require_open()
        stage = self._validate_spec(spec)
        receipt = stage / "supervisor" / _ATTEMPT_FILE
        if not receipt.exists():
            return self._make(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="provider attempt receipt is absent")
        recorded = self._load_spec(stage)
        if recorded != spec:
            return self._make(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="provider attempt receipt conflicts with controller spec")
        try:
            job_id = self.find_existing_job(spec)
        except SlurmUnavailable as exc:
            return self._make(
                spec, AttemptState.SUBMISSION_UNKNOWN, error=str(exc))
        if job_id is None:
            if self._intent_path(spec).exists():
                return self._make(
                    spec, AttemptState.SUBMISSION_UNKNOWN,
                    error=("durable submission intent exists but no scheduler "
                           "view currently exposes its job"))
            return self._make(
                spec, AttemptState.LOST,
                error=("both scheduler views confirm absence and no durable "
                       "submission intent exists"))
        script_digest = self._script_digest(spec)
        handle = self._handle(
            spec, job_id, recovered=True, script_digest=script_digest)
        state = self._batch_states((job_id,)).get(job_id)
        return self._observation(
            handle, spec, state, recovered_handle=handle)

    def reconcile_many(
        self,
        values: Iterable[
            ExternalHandle | AttemptSpec
            | tuple[ExternalHandle, AttemptSpec]],
    ) -> list[ProviderObservation]:
        """One `squeue` and one `sacct` for the whole batch, not per attempt.

        Polling a shared scheduler once per task is how a controller becomes a
        bad cluster citizen; the roadmap asks for batched reconcile and this is
        it.
        """
        self._require_open()
        raw_values = list(values)
        if not raw_values:
            return []
        normalized: list[tuple[ExternalHandle | None, AttemptSpec]] = []
        for value in raw_values:
            if isinstance(value, AttemptSpec):
                self._validate_spec(value)
                normalized.append((None, value))
                continue
            supplied_spec: AttemptSpec | None = None
            handle: ExternalHandle
            if (isinstance(value, tuple) and len(value) == 2
                    and isinstance(value[0], ExternalHandle)
                    and isinstance(value[1], AttemptSpec)):
                handle, supplied_spec = value
            elif isinstance(value, ExternalHandle):
                handle = value
            else:
                raise TypeError(
                    "reconcile_many values must be handles or AttemptSpecs")
            recorded = self._spec_for_handle(handle)
            if supplied_spec is not None:
                self._validate_spec(supplied_spec)
                if supplied_spec != recorded:
                    raise ValueError(
                        "supplied spec disagrees with handle receipt")
            normalized.append((handle, recorded))
        job_ids = [handle.external_id for handle, _spec in normalized
                   if handle is not None]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("reconcile_many cannot repeat a SLURM handle")
        states = self._batch_states(job_ids) if job_ids else {}
        observations: list[ProviderObservation] = []
        for handle, spec in normalized:
            if handle is None:
                observations.append(self.reconcile_token(spec))
            else:
                observations.append(self._observation(
                    handle, spec, states.get(handle.external_id)))
        return observations

    def _batch_states(self, job_ids: Sequence[str]) -> dict[str, str]:
        requested = tuple(_job_id(item) for item in job_ids)
        if len(requested) != len(set(requested)):
            raise ValueError("batched SLURM job IDs must be unique")
        if not requested:
            return {}
        joined = ",".join(requested)
        queue_states: dict[str, str] = {}
        accounting_states: dict[str, str] = {}
        queued = self._run(
            ["squeue", "--noheader", "--jobs", joined, "--format=%i %T",
             "--states=all"])
        if queued.ok:
            for line in queued.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and "." not in parts[0]:
                    job_id = _job_id(parts[0].strip())
                    if job_id not in requested:
                        raise SlurmUnavailable(
                            "squeue returned an unrequested job identity")
                    state = _scheduler_state(parts[1])
                    previous = queue_states.setdefault(job_id, state)
                    if previous != state:
                        raise SlurmUnavailable(
                            "squeue returned conflicting states for one job")
        accounted = self._run(
            ["sacct", "--noheader", "--parsable2", "--jobs", joined,
             "--format=JobID,State"])
        if accounted.ok:
            for line in accounted.stdout.strip().splitlines():
                fields = line.split("|")
                if len(fields) >= 2 and fields[0] and "." not in fields[0]:
                    job_id = _job_id(fields[0].strip())
                    if job_id not in requested:
                        raise SlurmUnavailable(
                            "sacct returned an unrequested job identity")
                    state = _scheduler_state(fields[1])
                    previous = accounting_states.setdefault(job_id, state)
                    if previous != state:
                        raise SlurmUnavailable(
                            "sacct returned conflicting states for one job")
        states: dict[str, str] = {}
        for job_id in requested:
            queue_state = queue_states.get(job_id)
            accounting_state = accounting_states.get(job_id)
            if (queue_state is not None and accounting_state is not None
                    and queue_state != accounting_state):
                states[job_id] = (
                    "VISIBILITY_MISMATCH:" + queue_state + ":"
                    + accounting_state)
            elif queue_state is not None:
                states[job_id] = queue_state
            elif accounting_state is not None:
                states[job_id] = accounting_state
        return states

    def _observation(
            self, handle: ExternalHandle, spec: AttemptSpec,
            state: str | None, *,
            recovered_handle: ExternalHandle | None = None,
    ) -> ProviderObservation:
        stage = self._validate_handle(spec, handle)
        if state is None:
            # Neither the queue nor accounting knows this job. That is not
            # evidence it never ran, so it must not become a resubmission.
            return self._make(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="job id is unknown to squeue and sacct",
                recovered_handle=recovered_handle)
        if state.startswith("VISIBILITY_MISMATCH:"):
            return self._make(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error=("squeue and sacct report conflicting states for the "
                       f"same job: {state.split(':', 1)[1]}"),
                recovered_handle=recovered_handle)
        if state in _ACTIVE_STATES:
            return self._make(
                spec,
                AttemptState.RUNNING if state == "RUNNING"
                else AttemptState.SUBMITTED,
                recovered_handle=recovered_handle)
        if state in _COMPLETED_STATES:
            result = stage / _RESULT_FILE
            decoded = _read_json(result)
            if _valid_result_manifest(decoded, spec, stage):
                return self._make(
                    spec, AttemptState.RESULT_READY,
                    result_manifest_path=str(result), exit_code=0,
                    recovered_handle=recovered_handle)
            # SLURM says the job exited zero but the worker published no
            # result marker: that is a failure, not a success.
            return self._make(
                spec, AttemptState.FAILED,
                error=("job completed without publishing a valid result "
                       "manifest"), exit_code=1,
                recovered_handle=recovered_handle)
        if state in _CANCELLED_STATES:
            return self._make(
                spec, AttemptState.CANCELLED, error="job was cancelled",
                recovered_handle=recovered_handle)
        if state in _FAILED_STATES:
            return self._make(
                spec, AttemptState.FAILED,
                error=f"job terminated as {state}", exit_code=1,
                recovered_handle=recovered_handle)
        return self._make(
            spec, AttemptState.SUBMISSION_UNKNOWN,
            error=f"unmapped SLURM state {state!r}",
            recovered_handle=recovered_handle)

    # -- cancel ----------------------------------------------------------

    def cancel(self, handle: ExternalHandle, *,
               grace_s: float | None = None) -> CommandResult:
        self._require_open()
        if not isinstance(handle, ExternalHandle):
            raise TypeError("cancel requires an ExternalHandle")
        self._spec_for_handle(handle)
        if (grace_s is not None
                and (isinstance(grace_s, bool)
                     or not isinstance(grace_s, (int, float))
                     or not math.isfinite(float(grace_s))
                     or grace_s < 0)):
            raise ValueError("cancel grace_s must be finite and non-negative")
        argv = ["scancel"]
        if grace_s is not None and grace_s > 0:
            argv.append(f"--signal=TERM")
        argv.append(handle.external_id)
        result = self._run(argv)
        if not result.ok:
            raise SlurmUnavailable(
                "scancel did not accept the exact recorded job: "
                f"{result.stderr.strip() or result.stdout.strip()}")
        return result

    # -- orphans ---------------------------------------------------------

    def find_orphans(self, known_tokens: Iterable[str]) -> tuple[str, ...]:
        """Jobs of ours that no live attempt claims.

        Basic correctness only: the roadmap puts fleet-wide tooling in Stage 11.
        """
        self._require_open()
        known = {str(item) for item in known_tokens}
        if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in known):
            raise ValueError("known attempt tokens must be SHA-256 digests")
        result = self._run(
            ["squeue", "--noheader", "--user", _current_user(),
             "--format=%i %j", "--states=all"])
        if not result.ok:
            raise SlurmUnavailable(
                f"squeue could not list our jobs: {result.stderr.strip()}")
        orphans = []
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 2 or not parts[1].startswith(JOB_NAME_PREFIX):
                continue
            token = parts[1][len(JOB_NAME_PREFIX):]
            if token not in known:
                orphans.append(_job_id(parts[0].strip()))
        return tuple(sorted(orphans))

    # -- helpers ---------------------------------------------------------

    def _handle(self, spec: AttemptSpec, job_id: str, *, recovered: bool,
                script_digest: str) -> ExternalHandle:
        stage = self._validate_spec(spec)
        validated_job_id = _job_id(job_id)
        if not re.fullmatch(r"[0-9a-f]{64}", script_digest):
            raise ValueError("SLURM script digest must be a SHA-256 digest")
        receipt = {
            "schema": "stage8r-slurm-job-receipt-v1",
            "provider": self.name,
            "external_id": validated_job_id,
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "job_name": job_name_for(spec),
            "stage_dir": str(stage),
            "script_sha256": script_digest,
            "site_snapshot_id": self.site.snapshot_id,
        }
        _write_once(
            stage / "supervisor" / _JOB_RECEIPT,
            (strict_canonical_json(receipt) + "\n").encode("ascii"),
            mode=0o600,
        )
        return ExternalHandle(
            provider=self.name,
            external_id=validated_job_id,
            attempt_id=spec.attempt_id,
            attempt_token=spec.attempt_token,
            # Node placement is operator information only. It is deliberately
            # absent from anything identity-bearing.
            metadata={
                "job_name": job_name_for(spec),
                "stage_dir": str(stage),
                "recovered": "true" if recovered else "false",
                "script_sha256": script_digest,
                "site_snapshot_id": self.site.snapshot_id,
            },
        )

    def _make(self, spec: AttemptSpec, state: AttemptState, *,
              result_manifest_path: str | None = None,
              error: str | None = None,
              exit_code: int | None = None,
              recovered_handle: ExternalHandle | None = None,
              ) -> ProviderObservation:
        return ProviderObservation(
            attempt_id=spec.attempt_id,
            attempt_token=spec.attempt_token,
            state=state,
            observed_at=time.time(),
            result_manifest_path=result_manifest_path,
            error=error,
            exit_code=exit_code,
            recovered_handle=recovered_handle,
        )

    def _load_spec(self, stage: Path) -> AttemptSpec:
        attempt_path = stage / "supervisor" / _ATTEMPT_FILE
        if attempt_path.is_symlink() or not attempt_path.is_file():
            raise ValueError("SLURM handle has no regular immutable attempt receipt")
        raw = strict_json_loads(attempt_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("SLURM attempt receipt must be an object")
        spec = AttemptSpec.from_dict(strict_copy(raw))
        recorded_stage = self._validate_spec(spec)
        if recorded_stage != stage:
            raise ValueError("SLURM attempt receipt names another stage directory")
        return spec

    def _spec_for_handle(self, handle: ExternalHandle) -> AttemptSpec:
        if not isinstance(handle, ExternalHandle):
            raise TypeError("SLURM provider requires an ExternalHandle")
        if handle.provider != self.name:
            raise ValueError(
                f"handle belongs to provider {handle.provider!r}, "
                f"expected {self.name!r}")
        stage_raw = handle.metadata.get("stage_dir")
        if not isinstance(stage_raw, str):
            raise ValueError("SLURM handle has no stage_dir")
        stage = self._stage_dir(stage_raw)
        spec = self._load_spec(stage)
        self._validate_handle(spec, handle)
        return spec

    def _validate_handle(self, spec: AttemptSpec,
                         handle: ExternalHandle) -> Path:
        stage = self._validate_spec(spec)
        if not isinstance(handle, ExternalHandle):
            raise TypeError("SLURM provider requires an ExternalHandle")
        if handle.provider != self.name:
            raise ValueError("SLURM handle provider does not match this provider")
        _job_id(handle.external_id)
        if (handle.attempt_id != spec.attempt_id
                or handle.attempt_token != spec.attempt_token):
            raise ValueError("SLURM handle does not bind the exact attempt")
        expected_fields = {
            "job_name", "stage_dir", "recovered", "script_sha256",
            "site_snapshot_id",
        }
        if set(handle.metadata) != expected_fields:
            raise ValueError("SLURM handle metadata fields are not exact")
        if handle.metadata["job_name"] != job_name_for(spec):
            raise ValueError("SLURM handle job name does not bind the attempt token")
        if handle.metadata["stage_dir"] != str(stage):
            raise ValueError("SLURM handle stage directory does not bind the attempt")
        if handle.metadata["recovered"] not in {"true", "false"}:
            raise ValueError("SLURM handle recovered marker is invalid")
        if handle.metadata["site_snapshot_id"] != self.site.snapshot_id:
            raise ValueError("SLURM handle site binding does not match this provider")
        script_digest = self._script_digest(spec)
        if handle.metadata["script_sha256"] != script_digest:
            raise ValueError("SLURM handle script identity does not verify")
        receipt_path = stage / "supervisor" / _JOB_RECEIPT
        receipt = _read_json(receipt_path)
        expected_receipt = {
            "schema": "stage8r-slurm-job-receipt-v1",
            "provider": self.name,
            "external_id": handle.external_id,
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "job_name": job_name_for(spec),
            "stage_dir": str(stage),
            "script_sha256": script_digest,
            "site_snapshot_id": self.site.snapshot_id,
        }
        if receipt != expected_receipt:
            raise ValueError("SLURM handle does not match its durable job receipt")
        return stage

    def _script_digest(self, spec: AttemptSpec) -> str:
        stage = self._validate_spec(spec)
        path = stage / "supervisor" / "job.sh"
        if path.is_symlink() or not path.is_file():
            raise ValueError("SLURM attempt script is absent or not regular")
        expected = self._job_script(spec, stage)
        actual = path.read_bytes()
        if actual != expected:
            raise ValueError("SLURM attempt script no longer matches its spec")
        return hashlib.sha256(actual).hexdigest()

    def _log_path(self, spec: AttemptSpec) -> Path:
        return Path(spec.stage_dir) / "supervisor" / "slurm.log"

    def _script_path(self, spec: AttemptSpec) -> Path:
        return Path(spec.stage_dir) / "supervisor" / "job.sh"

    def _intent_path(self, spec: AttemptSpec) -> Path:
        self._validate_spec(spec)
        return self.root / "submission-intents" / f"{spec.attempt_token}.json"

    def _intent_payload(self, spec: AttemptSpec,
                        script_digest: str) -> dict[str, str]:
        stage = self._validate_spec(spec)
        if not re.fullmatch(r"[0-9a-f]{64}", script_digest):
            raise ValueError("SLURM script digest must be a SHA-256 digest")
        return {
            "schema": "stage8r-slurm-submission-intent-v1",
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "job_name": job_name_for(spec),
            "provider": self.name,
            "site_snapshot_id": self.site.snapshot_id,
            "stage_dir": str(stage),
            "spec_sha256": strict_hash(spec.to_dict()),
            "script_sha256": script_digest,
        }

    def _create_submission_intent(
            self, spec: AttemptSpec,
            script_digest: str | None = None) -> bool:
        """Persist a single pre-sbatch intent; return whether this call won."""
        self._require_open()
        if script_digest is None:
            _stage, script_digest = self._prepare_attempt(spec)
        elif script_digest != self._script_digest(spec):
            raise ValueError("submission intent script identity does not verify")
        path = self._intent_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise SlurmUnavailable(
                "submission-intents must be a real provider-owned directory")
        # Persist creation of the submission-intents directory itself before
        # relying on a file inside it as the no-duplicate fence.  Fsyncing only
        # the child directory does not make its entry in ``root`` durable
        # across a power loss on all POSIX filesystems.
        parent_directory = os.open(
            path.parent.parent,
            os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)),
        )
        try:
            os.fsync(parent_directory)
        finally:
            os.close(parent_directory)
        payload = self._intent_payload(spec, script_digest)
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise SlurmUnavailable(
                    "durable submission intent is not a regular file")
            existing = require_object_fields(
                strict_json_loads(path.read_text(encoding="utf-8")),
                set(payload), "SlurmSubmissionIntent")
            if existing != payload:
                raise SlurmUnavailable(
                    "durable submission intent identity is corrupt")
            return False
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(strict_canonical_json(payload))
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(
                path.parent,
                os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            # The file may exist partially.  Keeping it is fail-safe: a later
            # process will refuse to submit rather than erase uncertain intent.
            raise
        return True


def _job_id(value: str) -> str:
    if not isinstance(value, str) or _JOB_ID.fullmatch(value) is None:
        raise ValueError("SLURM job ID must be a positive base-job integer")
    return value


def _scheduler_state(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("SLURM state must be non-empty text")
    # sacct may append detail (for example ``CANCELLED by 123``) and SLURM
    # may append ``+`` when its display column truncates a value.  Neither is
    # part of the state identity consumed by this provider.
    state = value.strip().upper().split()[0].rstrip("+")
    if not re.fullmatch(r"[A-Z][A-Z_]*", state):
        raise ValueError("SLURM state contains unsupported characters")
    return state


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        decoded = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return strict_copy(decoded)


def _write_once(path: Path, payload: bytes, *, mode: int) -> bool:
    """Atomically publish exact immutable bytes without following symlinks."""
    if not isinstance(payload, bytes):
        raise TypeError("immutable SLURM receipt payload must be bytes")
    if isinstance(mode, bool) or not isinstance(mode, int):
        raise TypeError("immutable SLURM receipt mode must be an integer")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("immutable SLURM receipt parent must be a directory")
    if path.is_symlink():
        raise ValueError("immutable SLURM receipt cannot be a symlink")
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            created = True
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError(
                    "immutable SLURM receipt target is not a regular file")
            if path.read_bytes() != payload:
                raise ValueError("immutable SLURM receipt content conflicts")
            created = False
        directory = os.open(
            path.parent,
            os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return created
    finally:
        temporary.unlink(missing_ok=True)


def _current_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # pragma: no cover - unusual environments
        import os
        return os.environ.get("USER", "unknown")


__all__ = [
    "JOB_NAME_PREFIX",
    "PROVIDER_NAME",
    "CommandResult",
    "CommandRunner",
    "SchedulerVisibility",
    "SchedulerVisibilityStatus",
    "SlurmProvider",
    "SlurmSubmitOptions",
    "SlurmUnavailable",
    "job_name_for",
    "real_command_runner",
    "slurm_is_available",
]
