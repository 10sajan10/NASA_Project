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

import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from engine.runtime.slurm import (
    JOB_NAME_PREFIX,
    CommandResult,
    SlurmProvider,
    SlurmSubmitOptions,
    SlurmUnavailable,
    SchedulerVisibilityStatus,
    job_name_for,
    slurm_is_available,
)
from engine.runtime.controller import WorkflowController
from engine.runtime.operations import operation_component
from engine.runtime.site import current_private_site
from engine.runtime.types import (
    AttemptSpec,
    AttemptState,
    BoundExecutionGraph,
    ExternalHandle,
    ResourceRequest,
    RunState,
    TaskTemplate,
    attempt_id,
    deployment_id,
)


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
        self.sbatch_malformed_after_landing = False
        self.scancel_broken = False
        self.execute_scripts = False
        self.last_worker: subprocess.CompletedProcess[str] | None = None

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
        if self.sbatch_malformed_after_landing:
            return CommandResult(0, "not-a-job-id\n", "")
        if self.execute_scripts:
            self.last_worker = subprocess.run(
                [argv[-1]], capture_output=True, text=True, check=False,
                timeout=10)
            self.finish(
                job_id,
                "COMPLETED" if self.last_worker.returncode == 0 else "FAILED")
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
        if self.scancel_broken:
            return CommandResult(1, "", "job cancellation rejected")
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


def _graph(seed: str = "one", *, value: int = 1
           ) -> BoundExecutionGraph:
    return BoundExecutionGraph.bind(
        f"fake-slurm-{seed}",
        (TaskTemplate(
            key="source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": value},
            resources=ResourceRequest(memory_mb=32, walltime_s=5),
        ),),
    )


def _spec(provider: SlurmProvider, seed: str = "one", *,
          stage_dir: Path | None = None) -> AttemptSpec:
    graph = _graph(seed)
    task = graph.task_by_key("source")
    run_id = f"fake-slurm-run-{seed}"
    deployment = deployment_id(graph.plan_id, provider.site, provider.name)
    aid = attempt_id(run_id, task.task_id, deployment, 1, 1)
    stage = stage_dir or provider.root / "staging" / aid
    return AttemptSpec(
        run_id=run_id,
        deployment_id=deployment,
        task=task,
        attempt_id=aid,
        attempt_number=1,
        fencing_token=1,
        provider=provider.name,
        input_artifacts={},
        stage_dir=str(stage),
        created_at=time.time(),
    )


@pytest.fixture()
def setup(tmp_path):
    fake = FakeSlurm()
    site = current_private_site(memory_limit_mb=1024)
    provider = SlurmProvider(tmp_path / "slurm", runner=fake,
                             require_tools=False,
                             site=site,
                             python_executable=Path(sys.executable),
                             options=SlurmSubmitOptions(
                                 time_limit="00:05:00"))
    spec = _spec(provider)
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


def test_malformed_sbatch_success_after_landing_recovers_by_token(setup):
    fake, provider, spec = setup
    fake.sbatch_malformed_after_landing = True

    handle = provider.submit(spec)

    assert fake.submissions == 1
    assert handle.external_id in fake.jobs
    assert handle.metadata["recovered"] == "true"


def test_an_unanswerable_cluster_never_becomes_a_resubmission(setup):
    """squeue and sacct both down: refuse, do not guess."""
    fake, provider, spec = setup
    fake.squeue_broken = True
    fake.sacct_broken = True

    with pytest.raises(SlurmUnavailable, match="absence is not proven"):
        provider.submit(spec)
    assert fake.submissions == 0


@pytest.mark.parametrize("broken", ["squeue", "sacct"])
def test_one_absent_view_and_one_unavailable_view_cannot_authorize_submit(
        setup, broken):
    fake, provider, spec = setup
    setattr(fake, f"{broken}_broken", True)

    with pytest.raises(SlurmUnavailable, match="absence is not proven"):
        provider.submit(spec)

    assert fake.submissions == 0


def test_independent_visibility_records_do_not_collapse_partial_failure(setup):
    fake, provider, spec = setup
    fake.sacct_broken = True

    queue_view, accounting_view = provider.lookup_existing_job(spec)

    assert queue_view.status is SchedulerVisibilityStatus.CONFIRMED_ABSENT
    assert accounting_view.status \
        is SchedulerVisibilityStatus.UNAVAILABLE_OR_INCOMPLETE


@pytest.mark.parametrize("source", ["squeue", "sacct"])
def test_multiple_jobs_for_one_attempt_token_fail_closed(setup, source):
    fake, provider, spec = setup
    original = fake.__call__

    def ambiguous(argv):
        if argv[0] == source:
            if source == "squeue":
                return CommandResult(0, "101\n202\n", "")
            return CommandResult(0, "101|COMPLETED\n202|FAILED\n", "")
        return original(argv)

    provider._run = ambiguous
    with pytest.raises(SlurmUnavailable, match="more than one job"):
        provider.lookup_existing_job(spec)


def test_scheduler_views_that_find_different_jobs_fail_closed(setup):
    fake, provider, spec = setup

    def disagree(argv):
        if argv[0] == "squeue":
            return CommandResult(0, "101\n", "")
        if argv[0] == "sacct":
            return CommandResult(0, "202|COMPLETED\n", "")
        return fake(argv)

    provider._run = disagree
    with pytest.raises(SlurmUnavailable, match="disagree"):
        provider.find_existing_job(spec)


def test_a_preexisting_submission_intent_never_becomes_a_second_sbatch(setup):
    fake, provider, spec = setup
    assert provider._create_submission_intent(spec)

    with pytest.raises(SlurmUnavailable, match="prior durable submission intent"):
        provider.submit(spec)

    assert fake.submissions == 0


def test_an_invisible_accepted_job_remains_submission_unknown(setup):
    fake, provider, spec = setup
    first = provider.submit(spec)
    assert first.external_id in fake.jobs
    # Simulate scheduler/accounting visibility lag without erasing the fact
    # that the first sbatch was accepted.
    fake.jobs.clear()

    with pytest.raises(SlurmUnavailable, match="prior durable submission intent"):
        provider.submit(spec)

    assert fake.submissions == 1


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
    assert "without publishing" in failed.error

    stage = Path(spec.stage_dir)
    payload = stage / "outputs" / "result" / "payload.json"
    payload.parent.mkdir(parents=True)
    payload.write_text("1\n", encoding="utf-8")
    (stage / "result.json").write_text(json.dumps({
        "schema": "stage1-attempt-result-v1",
        "attempt_id": spec.attempt_id,
        "attempt_token": spec.attempt_token,
        "outputs": {"result": {"path": "outputs/result/payload.json"}},
    }), encoding="utf-8")
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

    specs = [_spec(provider, f"batch-{i}") for i in range(5)]
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


def test_cancel_failure_is_not_reported_as_success(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.scancel_broken = True

    with pytest.raises(SlurmUnavailable, match="did not accept"):
        provider.cancel(handle)

    assert fake.jobs[handle.external_id]["state"] == "PENDING"


def test_orphans_are_our_jobs_that_no_attempt_claims(setup):
    fake, provider, spec = setup
    mine = provider.submit(spec)
    other = _spec(provider, "orphan")
    stray = provider.submit(other)
    # A job that is not ours at all must never be reported as an orphan.
    fake.jobs["9999"] = {"name": "someone-elses-job", "state": "RUNNING"}

    orphans = provider.find_orphans({spec.attempt_token})
    assert orphans == (stray.external_id,)
    assert mine.external_id not in orphans
    assert "9999" not in orphans


# -- strict provider boundary and controller bridge ----------------------


def test_foreign_provider_and_stage_root_are_rejected_before_scheduler_calls(
        setup, tmp_path):
    fake, provider, spec = setup
    foreign = dataclasses.replace(spec, provider="stage1-local-subprocess")
    escaped = dataclasses.replace(
        spec, stage_dir=str(tmp_path / "outside" / "attempt"))

    with pytest.raises(ValueError, match="expected 'stage9a-slurm'"):
        provider.submit(foreign)
    with pytest.raises(ValueError, match="escapes"):
        provider.submit(escaped)

    assert fake.submissions == 0


@pytest.mark.parametrize("mutation,match", [
    (lambda handle: dataclasses.replace(
        handle, provider="stage1-local-subprocess"), "provider"),
    (lambda handle: dataclasses.replace(
        handle, attempt_token="0" * 64), "exact attempt"),
    (lambda handle: dataclasses.replace(
        handle, external_id="9999"), "job receipt"),
    (lambda handle: dataclasses.replace(
        handle, metadata={**handle.metadata, "stage_dir": "/tmp/forged"}),
     "escapes"),
    (lambda handle: dataclasses.replace(
        handle, metadata={**handle.metadata, "site_snapshot_id": "0" * 64}),
     "site binding"),
])
def test_reconcile_rejects_forged_handles_before_querying_the_job(
        setup, mutation, match):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    calls = 0
    inner = provider._run

    def counting(argv):
        nonlocal calls
        calls += 1
        return inner(argv)

    provider._run = counting
    with pytest.raises(ValueError, match=match):
        provider.reconcile(mutation(handle))
    assert calls == 0


def test_cancel_rejects_a_handle_with_a_forged_job_id(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    forged = dataclasses.replace(handle, external_id="9999")

    with pytest.raises(ValueError, match="job receipt"):
        provider.cancel(forged)

    assert fake.jobs[handle.external_id]["state"] == "PENDING"
    assert "9999" not in fake.jobs


def test_job_script_is_attempt_private_deterministic_and_closed(setup):
    _fake, provider, spec = setup
    stage, first_digest = provider._prepare_attempt(spec)
    first = (stage / "supervisor" / "job.sh").read_bytes()
    repeated_stage, repeated_digest = provider._prepare_attempt(spec)

    assert repeated_stage == stage
    assert repeated_digest == first_digest
    assert (repeated_stage / "supervisor" / "job.sh").read_bytes() == first
    decoded = first.decode("utf-8")
    assert decoded.startswith("#!/bin/sh\nset -eu\n")
    assert "-m engine.runtime.worker" in decoded
    assert f"--runtime-root {provider.root}" in decoded
    assert f"--stage-dir {stage}" in decoded
    assert f"--attempt-token {spec.attempt_token}" in decoded
    assert "--provider-name stage9a-slurm" in decoded
    assert "srun" not in decoded


def test_modified_attempt_script_invalidates_reconcile_before_scheduler_query(
        setup):
    _fake, provider, spec = setup
    handle = provider.submit(spec)
    script = Path(spec.stage_dir) / "supervisor" / "job.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    calls = 0
    inner = provider._run

    def counting(argv):
        nonlocal calls
        calls += 1
        return inner(argv)

    provider._run = counting
    with pytest.raises(ValueError, match="no longer matches"):
        provider.reconcile(handle)
    assert calls == 0


def test_reconcile_accepts_a_spec_and_recovers_the_durable_handle(setup):
    _fake, provider, spec = setup
    submitted = provider.submit(spec)

    observation = provider.reconcile(spec)

    assert observation.state is AttemptState.SUBMITTED
    assert observation.recovered_handle is not None
    assert observation.recovered_handle.external_id == submitted.external_id
    assert observation.recovered_handle.metadata["recovered"] == "true"


def test_conflicting_queue_and_accounting_states_are_unknown(setup):
    fake, provider, spec = setup
    handle = provider.submit(spec)
    fake.accounting[handle.external_id] = {
        "name": job_name_for(spec), "state": "RUNNING"}

    observation = provider.reconcile(handle)

    assert observation.state is AttemptState.SUBMISSION_UNKNOWN
    assert "conflicting states" in observation.error


def test_closed_provider_refuses_state_changing_and_reconcile_calls(setup):
    _fake, provider, spec = setup
    handle = provider.submit(spec)
    provider.close()

    assert provider.execution_sites() == (provider.site,)
    with pytest.raises(RuntimeError, match="closed"):
        provider.submit(spec)
    with pytest.raises(RuntimeError, match="closed"):
        provider.reconcile(handle)
    with pytest.raises(RuntimeError, match="closed"):
        provider.cancel(handle)


def test_fake_slurm_controller_executes_worker_and_commits_artifact(tmp_path):
    runtime_root = tmp_path / "runtime"
    fake = FakeSlurm()
    fake.execute_scripts = True
    site = current_private_site(memory_limit_mb=1024)
    provider = SlurmProvider(
        runtime_root,
        runner=fake,
        require_tools=False,
        site=site,
        python_executable=Path(sys.executable),
        options=SlurmSubmitOptions(time_limit="00:05:00"),
    )
    graph = _graph("controller", value=42)
    task = graph.task_by_key("source")

    with WorkflowController(
            runtime_root, site=site, provider=provider,
            poll_interval_s=0.001) as controller:
        run_id = controller.create_run(graph, run_id="fake-slurm-controller")
        assert controller.run_until_terminal(
            run_id, timeout_s=10) is RunState.SUCCEEDED
        assert controller.output_value(run_id, task.task_id) == 42

    assert fake.submissions == 1
    assert fake.last_worker is not None
    assert fake.last_worker.returncode == 0, fake.last_worker.stderr


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
