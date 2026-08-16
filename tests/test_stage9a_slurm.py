"""Stage-9A SLURM provider: recovery that never blind-resubmits.

The exit gate that matters is the one about duplicates. A controller that
crashes between `sbatch` returning and the handle reaching disk must not run
the science twice, and when the cluster cannot say whether a job exists the
answer must be `SUBMISSION_UNKNOWN` rather than a fresh submission.

These tests drive a fake SLURM through the provider's command seam, so the
whole state machine and every crash window are exercised without queuing work
on a shared cluster. Command *shapes* are asserted against the real client
tools separately.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from engine.runtime.slurm import (
    JOB_NAME_PREFIX,
    CommandResult,
    SlurmProvider,
    SlurmSubmitOptions,
    SlurmUnavailable,
    job_name_for,
    slurm_is_available,
)
from engine.runtime.types import AttemptState


class FakeSlurm:
    """A queue that behaves like SLURM for the paths we depend on."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, str]] = {}       # id -> {name, state}
        self.accounting: dict[str, dict[str, str]] = {}
        self.next_id = 1000
        self.submissions = 0
        self.squeue_broken = False
        self.sacct_broken = False
        self.sbatch_fails_after_landing = False

    def __call__(self, argv):
        tool = argv[0]
        if tool == "sbatch":
            return self._sbatch(argv)
        if tool == "squeue":
            return self._squeue(argv)
        if tool == "sacct":
            return self._sacct(argv)
        if tool == "scancel":
            return self._scancel(argv)
        raise AssertionError(f"unexpected tool {tool}")

    def _flag(self, argv, prefix):
        for item in argv:
            if item.startswith(prefix):
                return item[len(prefix):]
        return None

    def _sbatch(self, argv):
        self.submissions += 1
        name = self._flag(argv, "--job-name=")
        job_id = str(self.next_id)
        self.next_id += 1
        self.jobs[job_id] = {"name": name, "state": "PENDING"}
        if self.sbatch_fails_after_landing:
            # The job really landed, but our client never saw the id.
            return CommandResult(1, "", "srun: error: connection reset")
        return CommandResult(0, f"{job_id};cluster\n", "")

    def _squeue(self, argv):
        if self.squeue_broken:
            return CommandResult(1, "", "slurm_load_jobs error")
        name = self._value(argv, "--name")
        jobs = self._value(argv, "--jobs")
        user = self._value(argv, "--user")
        lines = []
        for job_id, record in sorted(self.jobs.items()):
            if name is not None and record["name"] != name:
                continue
            if jobs is not None and job_id not in jobs.split(","):
                continue
            if "%i %T" in " ".join(argv):
                lines.append(f"{job_id} {record['state']}")
            elif "%i %j" in " ".join(argv):
                lines.append(f"{job_id} {record['name']}")
            else:
                lines.append(job_id)
        _ = user
        return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""), "")

    def _sacct(self, argv):
        if self.sacct_broken:
            return CommandResult(1, "", "sacct: error: Problem talking to db")
        name = self._value(argv, "--name")
        jobs = self._value(argv, "--jobs")
        lines = []
        for job_id, record in sorted(self.accounting.items()):
            if name is not None and record["name"] != name:
                continue
            if jobs is not None and job_id not in jobs.split(","):
                continue
            lines.append(f"{job_id}|{record['state']}")
        return CommandResult(0, "\n".join(lines) + ("\n" if lines else ""), "")

    def _scancel(self, argv):
        job_id = argv[-1]
        if job_id in self.jobs:
            self.jobs[job_id]["state"] = "CANCELLED"
        return CommandResult(0, "", "")

    def _value(self, argv, flag):
        for index, item in enumerate(argv):
            if item == flag and index + 1 < len(argv):
                return argv[index + 1]
            if item.startswith(f"{flag}="):
                return item.split("=", 1)[1]
        return None

    # -- scenario helpers -------------------------------------------------

    def finish(self, job_id: str, state: str = "COMPLETED") -> None:
        record = self.jobs.pop(job_id)
        self.accounting[job_id] = {"name": record["name"], "state": state}

    def set_state(self, job_id: str, state: str) -> None:
        self.jobs[job_id]["state"] = state


def _digest(seed: str) -> str:
    """Attempt ids and tokens are SHA-256 digests in this runtime."""
    import hashlib
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


class _Spec:
    """The minimum AttemptSpec surface this provider touches."""

    def __init__(self, stage_dir: Path, token: str = "tok-1",
                 attempt_id: str = "att-1") -> None:
        self.stage_dir = str(stage_dir)
        self.attempt_token = _digest(token)
        self.attempt_id = _digest(attempt_id)


@pytest.fixture()
def setup(tmp_path):
    fake = FakeSlurm()
    provider = SlurmProvider(tmp_path / "slurm", runner=fake,
                             require_tools=False,
                             options=SlurmSubmitOptions(time_limit="00:05:00"))
    spec = _Spec(tmp_path / "stage")
    (tmp_path / "stage" / "supervisor").mkdir(parents=True)
    return fake, provider, spec


# -- identity -------------------------------------------------------------


def test_the_job_name_is_the_recovery_key(setup):
    _fake, _provider, spec = setup
    assert job_name_for(spec) == f"{JOB_NAME_PREFIX}{spec.attempt_token}"
    # Derived only from the token, so it survives any node placement.
    assert spec.attempt_token in job_name_for(spec)


def test_placement_never_enters_identity(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    blob = repr(handle.metadata) + handle.external_id + handle.attempt_token
    for leaked in ("node", "nid", "partition"):
        assert f"{leaked}=" not in blob
    assert handle.attempt_token == spec.attempt_token
    _ = fake


# -- the duplicate-submission window --------------------------------------


def test_a_crash_after_sbatch_recovers_instead_of_resubmitting(setup):
    """The window the whole design exists for."""
    fake, provider, spec = setup
    first = provider.submit(spec)
    assert fake.submissions == 1

    # Controller dies before persisting `first`; a new controller retries.
    second = provider.submit(spec)
    assert fake.submissions == 1              # no second sbatch
    assert second.external_id == first.external_id
    assert second.metadata["recovered"] == "true"


def test_recovery_finds_a_job_that_already_finished(setup):
    """After completion the job leaves squeue but remains in sacct."""
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.finish(handle.external_id, "COMPLETED")

    again = provider.submit(spec)
    assert fake.submissions == 1
    assert again.external_id == handle.external_id


def test_sbatch_reporting_failure_after_the_job_landed_does_not_duplicate(setup):
    """A lost client connection is not evidence the job did not start."""
    fake, provider, spec = setup
    fake.sbatch_fails_after_landing = True

    handle = provider.submit(spec)
    assert fake.submissions == 1
    assert handle.metadata["recovered"] == "true"


def test_an_unanswerable_cluster_never_becomes_a_resubmission(setup):
    """squeue and sacct both down: refuse, do not guess."""
    fake, provider, spec = setup
    fake.squeue_broken = True
    fake.sacct_broken = True

    with pytest.raises(SlurmUnavailable, match="neither squeue nor sacct"):
        provider.submit(spec)
    assert fake.submissions == 0


def test_a_partially_answerable_cluster_still_recovers(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.finish(handle.external_id, "COMPLETED")
    fake.squeue_broken = True                 # only accounting is reachable

    again = provider.submit(spec)
    assert fake.submissions == 1
    assert again.external_id == handle.external_id


# -- the state machine ----------------------------------------------------


def test_queued_and_running_map_to_submitted_and_running(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    assert provider.reconcile(handle, spec).state is AttemptState.SUBMITTED
    fake.set_state(handle.external_id, "RUNNING")
    assert provider.reconcile(handle, spec).state is AttemptState.RUNNING


def test_completion_requires_a_published_result_manifest(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.finish(handle.external_id, "COMPLETED")

    # SLURM says exit zero, but the worker published nothing: that is a
    # failure, not a success.
    failed = provider.reconcile(handle, spec)
    assert failed.state is AttemptState.FAILED
    assert "without publishing a result" in failed.error

    (Path(spec.stage_dir) / "result.json").write_text("{}", encoding="utf-8")
    ready = provider.reconcile(handle, spec)
    assert ready.state is AttemptState.RESULT_READY
    assert ready.result_manifest_path.endswith("result.json")


@pytest.mark.parametrize("slurm_state,expected", [
    ("FAILED", AttemptState.FAILED),
    ("TIMEOUT", AttemptState.FAILED),
    ("OUT_OF_MEMORY", AttemptState.FAILED),
    ("NODE_FAIL", AttemptState.FAILED),
    ("CANCELLED", AttemptState.CANCELLED),
])
def test_terminal_slurm_states_map_to_the_attempt_state_machine(
        setup, slurm_state, expected):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.finish(handle.external_id, slurm_state)
    assert provider.reconcile(handle, spec).state is expected


def test_an_unknown_job_is_submission_unknown_not_failed(setup):
    """Absence of evidence is not evidence the attempt never ran."""
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.jobs.clear()                         # vanished from both views

    observation = provider.reconcile(handle, spec)
    assert observation.state is AttemptState.SUBMISSION_UNKNOWN
    assert "unknown to squeue and sacct" in observation.error


def test_an_unmapped_state_is_unknown_rather_than_assumed_good(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.set_state(handle.external_id, "SOME_FUTURE_STATE")
    observation = provider.reconcile(handle, spec)
    assert observation.state is AttemptState.SUBMISSION_UNKNOWN
    assert "unmapped SLURM state" in observation.error


# -- batching, cancel, orphans -------------------------------------------


def test_reconcile_batches_one_query_for_many_attempts(setup):
    fake, provider, spec = setup
    calls: list[str] = []
    inner = fake.__call__

    def counting(argv):
        calls.append(argv[0])
        return inner(argv)
    provider._run = counting

    specs = [_Spec(Path(spec.stage_dir), token=f"tok-{i}", attempt_id=f"a{i}")
             for i in range(5)]
    handles = [provider.submit(item) for item in specs]
    calls.clear()

    observations = provider.reconcile_many(list(zip(handles, specs)))
    assert len(observations) == 5
    # One squeue for the whole batch, not one per attempt.
    assert calls.count("squeue") == 1


def test_cancel_targets_the_job_and_reports_success(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    assert provider.cancel(handle).ok
    assert fake.jobs[handle.external_id]["state"] == "CANCELLED"
    assert provider.reconcile(handle, spec).state is AttemptState.CANCELLED


def test_orphans_are_our_jobs_that_no_attempt_claims(setup):
    fake, provider, spec = setup
    mine = provider.submit(spec)
    other = _Spec(Path(spec.stage_dir), token="tok-orphan", attempt_id="a2")
    stray = provider.submit(other)
    # A job that is not ours at all must never be reported as an orphan.
    fake.jobs["9999"] = {"name": "someone-elses-job", "state": "RUNNING"}

    orphans = provider.find_orphans({spec.attempt_token})
    assert orphans == (stray.external_id,)
    assert mine.external_id not in orphans
    assert "9999" not in orphans


# -- shape checks against the real client tools ---------------------------


@pytest.mark.skipif(not slurm_is_available(),
                    reason="SLURM client tools are not installed here")
def test_the_flags_we_send_exist_in_the_installed_slurm():
    """Guard against inventing flags the real tools do not accept.

    This queries the installed client only; it queues no work.
    """
    import subprocess
    helps = {
        tool: subprocess.run([tool, "--help"], capture_output=True, text=True,
                             timeout=30).stdout
        for tool in ("sbatch", "squeue", "sacct")
    }
    for flag in ("--parsable", "--job-name", "--time", "--mem", "--output"):
        assert flag in helps["sbatch"], f"sbatch lacks {flag}"
    for flag in ("--noheader", "--name", "--states", "--format"):
        assert flag in helps["squeue"], f"squeue lacks {flag}"
    for flag in ("--noheader", "--parsable2", "--name", "--format"):
        assert flag in helps["sacct"], f"sacct lacks {flag}"
