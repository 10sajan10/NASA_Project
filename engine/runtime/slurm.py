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

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .types import AttemptSpec, AttemptState, ExternalHandle, ProviderObservation

PROVIDER_NAME = "stage9a-slurm"

# SLURM job names are the recovery index.  A stable, greppable prefix keeps
# `squeue --name` and `sacct --name` lookups exact rather than fuzzy.
JOB_NAME_PREFIX = "nasa-attempt-"

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
    """Whether the client tools exist on this machine."""
    return all(shutil.which(name) is not None
               for name in ("sbatch", "squeue", "sacct", "scancel"))


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
        if not self.time_limit.strip():
            raise ValueError("a SLURM time limit is required")

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
                 require_tools: bool = True) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("SLURM provider root must be absolute")
        self.root.mkdir(parents=True, exist_ok=True)
        self.options = options
        self._run = runner or real_command_runner()
        if require_tools and runner is None and not slurm_is_available():
            raise SlurmUnavailable(
                "sbatch/squeue/sacct/scancel are not on PATH; this provider "
                "requires a real SLURM installation")

    # -- lookup ----------------------------------------------------------

    def find_existing_job(self, spec: AttemptSpec) -> str | None:
        """Ask the cluster whether this attempt already has a job.

        Checks the live queue first, then accounting, because a job that
        finished between a crash and this call is no longer in `squeue` but is
        in `sacct`.  Raises rather than guessing when neither can answer.
        """
        name = job_name_for(spec)
        queued = self._run(
            ["squeue", "--noheader", "--name", name, "--format=%i",
             "--states=all"])
        if queued.ok:
            job_id = queued.stdout.strip().splitlines()
            if job_id:
                return job_id[0].strip().split(".")[0]
        accounted = self._run(
            ["sacct", "--noheader", "--parsable2", "--name", name,
             "--format=JobID,State"])
        if accounted.ok:
            for line in accounted.stdout.strip().splitlines():
                fields = line.split("|")
                if fields and fields[0] and "." not in fields[0]:
                    return fields[0].strip()
        if not queued.ok and not accounted.ok:
            raise SlurmUnavailable(
                "neither squeue nor sacct could answer whether attempt "
                f"{spec.attempt_id} already has a job: "
                f"{queued.stderr.strip()} / {accounted.stderr.strip()}")
        return None

    # -- submit ----------------------------------------------------------

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        """Submit, but only after confirming no job exists for this attempt.

        The pre-check is what makes a crash between `sbatch` and handle
        persistence survivable: the retry finds the existing job by token
        instead of starting a second one.
        """
        existing = self.find_existing_job(spec)
        if existing is not None:
            return self._handle(spec, existing, recovered=True)

        log_path = self._log_path(spec)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        argv = self.options.sbatch_arguments(spec, log_path)
        argv.append(str(self._script_path(spec)))
        result = self._run(argv)
        if not result.ok:
            # Submission may still have landed; re-ask before concluding.
            recheck = self.find_existing_job(spec)
            if recheck is not None:
                return self._handle(spec, recheck, recovered=True)
            raise SlurmUnavailable(
                f"sbatch failed for attempt {spec.attempt_id}: "
                f"{result.stderr.strip() or result.stdout.strip()}")
        job_id = result.stdout.strip().split(";")[0].strip()
        if not job_id:
            raise SlurmUnavailable("sbatch returned no job id")
        return self._handle(spec, job_id, recovered=False)

    # -- reconcile -------------------------------------------------------

    def reconcile(self, handle: ExternalHandle,
                  spec: AttemptSpec) -> ProviderObservation:
        return self.reconcile_many(((handle, spec),))[0]

    def reconcile_many(
        self,
        pairs: Iterable[tuple[ExternalHandle, AttemptSpec]],
    ) -> list[ProviderObservation]:
        """One `squeue` and one `sacct` for the whole batch, not per attempt.

        Polling a shared scheduler once per task is how a controller becomes a
        bad cluster citizen; the roadmap asks for batched reconcile and this is
        it.
        """
        values = list(pairs)
        if not values:
            return []
        job_ids = [handle.external_id for handle, _spec in values]
        states = self._batch_states(job_ids)
        observations = []
        for handle, spec in values:
            state = states.get(handle.external_id)
            observations.append(self._observation(handle, spec, state))
        return observations

    def _batch_states(self, job_ids: Sequence[str]) -> dict[str, str]:
        joined = ",".join(job_ids)
        states: dict[str, str] = {}
        queued = self._run(
            ["squeue", "--noheader", "--jobs", joined, "--format=%i %T",
             "--states=all"])
        if queued.ok:
            for line in queued.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 2 and "." not in parts[0]:
                    states[parts[0].strip()] = parts[1].strip().upper()
        missing = [item for item in job_ids if item not in states]
        if missing:
            accounted = self._run(
                ["sacct", "--noheader", "--parsable2", "--jobs",
                 ",".join(missing), "--format=JobID,State"])
            if accounted.ok:
                for line in accounted.stdout.strip().splitlines():
                    fields = line.split("|")
                    if len(fields) >= 2 and fields[0] and "." not in fields[0]:
                        # "CANCELLED by 12345" -> "CANCELLED"
                        states[fields[0].strip()] = (
                            fields[1].strip().upper().split()[0])
        return states

    def _observation(self, handle: ExternalHandle, spec: AttemptSpec,
                     state: str | None) -> ProviderObservation:
        if state is None:
            # Neither the queue nor accounting knows this job. That is not
            # evidence it never ran, so it must not become a resubmission.
            return self._make(spec, AttemptState.SUBMISSION_UNKNOWN,
                              error="job id is unknown to squeue and sacct")
        if state in _ACTIVE_STATES:
            return self._make(
                spec,
                AttemptState.RUNNING if state == "RUNNING"
                else AttemptState.SUBMITTED)
        if state in _COMPLETED_STATES:
            result = Path(spec.stage_dir) / "result.json"
            if result.exists():
                return self._make(spec, AttemptState.RESULT_READY,
                                  result_manifest_path=str(result))
            # SLURM says the job exited zero but the worker published no
            # result marker: that is a failure, not a success.
            return self._make(
                spec, AttemptState.FAILED,
                error="job completed without publishing a result manifest")
        if state in _CANCELLED_STATES:
            return self._make(spec, AttemptState.CANCELLED,
                              error="job was cancelled")
        if state in _FAILED_STATES:
            return self._make(spec, AttemptState.FAILED,
                              error=f"job terminated as {state}")
        return self._make(spec, AttemptState.SUBMISSION_UNKNOWN,
                          error=f"unmapped SLURM state {state!r}")

    # -- cancel ----------------------------------------------------------

    def cancel(self, handle: ExternalHandle, *,
               grace_s: float | None = None) -> CommandResult:
        argv = ["scancel"]
        if grace_s is not None and grace_s > 0:
            argv.append(f"--signal=TERM")
        argv.append(handle.external_id)
        return self._run(argv)

    # -- orphans ---------------------------------------------------------

    def find_orphans(self, known_tokens: Iterable[str]) -> tuple[str, ...]:
        """Jobs of ours that no live attempt claims.

        Basic correctness only: the roadmap puts fleet-wide tooling in Stage 11.
        """
        known = {str(item) for item in known_tokens}
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
                orphans.append(parts[0].strip())
        return tuple(sorted(orphans))

    # -- helpers ---------------------------------------------------------

    def _handle(self, spec: AttemptSpec, job_id: str,
                *, recovered: bool) -> ExternalHandle:
        return ExternalHandle(
            provider=self.name,
            external_id=str(job_id),
            attempt_id=spec.attempt_id,
            attempt_token=spec.attempt_token,
            # Node placement is operator information only. It is deliberately
            # absent from anything identity-bearing.
            metadata={
                "job_name": job_name_for(spec),
                "stage_dir": str(spec.stage_dir),
                "recovered": "true" if recovered else "false",
            },
        )

    def _make(self, spec: AttemptSpec, state: AttemptState, *,
              result_manifest_path: str | None = None,
              error: str | None = None) -> ProviderObservation:
        import time
        return ProviderObservation(
            attempt_id=spec.attempt_id,
            attempt_token=spec.attempt_token,
            state=state,
            observed_at=time.time(),
            result_manifest_path=result_manifest_path,
            error=error,
            exit_code=None,
            recovered_handle=None,
        )

    def _log_path(self, spec: AttemptSpec) -> Path:
        return Path(spec.stage_dir) / "supervisor" / "slurm.log"

    def _script_path(self, spec: AttemptSpec) -> Path:
        return Path(spec.stage_dir) / "supervisor" / "job.sh"


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
    "SlurmProvider",
    "SlurmSubmitOptions",
    "SlurmUnavailable",
    "job_name_for",
    "real_command_runner",
    "slurm_is_available",
]
