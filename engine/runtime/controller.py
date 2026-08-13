"""Event-driven, single-node WorkflowController for Stage 1.

The controller is the sole writer of authoritative runtime state.  Workers
return attempt-scoped files; only :class:`ArtifactCommitter` may make those
files visible to downstream tasks.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Callable

from .artifacts import ArtifactCommitter, CommitDisposition
from .identity import strict_copy, strict_hash
from .operations import operation_component
from .provider import LocalSubprocessProvider, SubmissionOutcomeUnknown
from .site import current_private_site, preflight_request, validate_runtime_root
from .state import AttemptRecord, ControllerLock, RuntimeStore
from .types import (
    AttemptSpec,
    AttemptState,
    BoundExecutionGraph,
    BoundTask,
    ProviderObservation,
    RunState,
    SiteSnapshot,
    TaskState,
    deployment_id,
)


class WorkflowController:
    """Durable FIFO controller for an already-bound execution graph.

    Stage 1 deliberately admits at most one attempt at a time.  Resource-aware
    packing and priority policies belong to Stage 8; keeping admission here
    serial makes the private-node envelope conservative and auditable.
    """

    def __init__(self, runtime_root: Path | str, *,
                 site: SiteSnapshot | None = None,
                 poll_interval_s: float = 0.02,
                 max_inflight: int = 1,
                 artifact_failpoint: Callable[[str], None] | None = None,
                 provider: LocalSubprocessProvider | None = None) -> None:
        root, fs_type = validate_runtime_root(Path(runtime_root))
        if max_inflight != 1:
            raise ValueError(
                "Stage 1 requires max_inflight=1; resource packing is deferred")
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        self.runtime_root = root
        self.filesystem_type = fs_type
        self.site = site or current_private_site()
        self.poll_interval_s = poll_interval_s
        self.max_inflight = max_inflight
        self.controller_id = uuid.uuid4().hex
        self._closed = False
        self._lock = ControllerLock(root / "control" / "controller.lock")
        try:
            self.store = RuntimeStore(root / "control" / "runtime.sqlite3")
            self.controller_epoch = self.store.start_controller_session(
                self.controller_id)
            self.provider = provider or LocalSubprocessProvider(
                root, site=self.site)
            if self.provider.root != root:
                raise ValueError("local provider root must equal the controller runtime root")
            if self.provider.site.snapshot_id != self.site.snapshot_id:
                raise ValueError("local provider site must equal the controller site binding")
            self.committer = ArtifactCommitter(
                root, self.store, failpoint=artifact_failpoint)
        except BaseException:
            if hasattr(self, "store") and hasattr(self, "controller_epoch"):
                try:
                    self.store.stop_controller_session(self.controller_id)
                except Exception:
                    pass
            self._lock.close()
            raise

    def create_run(self, graph: BoundExecutionGraph, *,
                   run_id: str | None = None) -> str:
        """Persist an immutable bound graph and start a new run."""
        self._require_open()
        return self.store.create_run(graph, run_id=run_id)

    def tick(self, run_id: str) -> RunState:
        """Advance one nonblocking controller iteration."""
        self._require_open()
        current = self.store.run_state(run_id)
        if current is not RunState.RUNNING:
            return current
        self.store.heartbeat(self.controller_id)
        self.store.consume_due_retry_wakes(run_id)
        self._recover_created_attempts(run_id)

        # Reconciliation precedes deadline handling so a completed result wins
        # if it was durably published by the worker before this observation.
        for record in self.store.active_attempts(run_id):
            self._reconcile_record(record)

        # Process exit success is merely RESULT_READY.  This is the only path
        # from staged bytes through validation and authoritative publication.
        for record in self.store.result_ready_attempts(run_id):
            self.committer.process(record)

        # The catalog is a rebuildable read model.  Projection failure must not
        # roll back or weaken the authoritative artifact commit above.
        self.store.project_catalog_outbox()

        # A deadline revokes the task fence before the provider is signalled.
        for record in self.store.due_deadline_attempts(run_id):
            cancelled = self.store.request_cancel(
                run_id, record.spec.task.task_id)
            if cancelled is not None:
                self._stop_cancelled_attempt(
                    cancelled, error="attempt deadline exceeded")

        if self._global_active_attempt_count() < self.max_inflight:
            ready = self.store.ready_tasks(run_id)
            if ready:
                self._dispatch(run_id, ready[0])

        return self.store.finalize_run_state(run_id)

    def run_until_terminal(self, run_id: str, *,
                           timeout_s: float = 30.0) -> RunState:
        """Run the event loop until the run is terminal or the caller times out."""
        self._require_open()
        if timeout_s <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout_s
        while True:
            state = self.tick(run_id)
            if state is not RunState.RUNNING:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"run {run_id} did not finish within {timeout_s:.3f}s")
            time.sleep(min(self.poll_interval_s,
                           max(0.0, deadline - time.monotonic())))

    def cancel_task(self, run_id: str, task_id: str) -> bool:
        """Fence a task, stop its local process, and retain an audit trail."""
        self._require_open()
        before = self.store.task_state(run_id, task_id)
        if before in {
                TaskState.SUCCEEDED, TaskState.FAILED,
                TaskState.INVALID_OUTPUT, TaskState.CANCELLED}:
            return False
        record = self.store.request_cancel(run_id, task_id)
        if record is None:
            self.store.finalize_run_state(run_id)
            return True
        self._stop_cancelled_attempt(record, error="cancelled")
        self.store.finalize_run_state(run_id)
        return True

    def output_value(self, run_id: str, task_id: str,
                     output_name: str = "result") -> object:
        """Read and independently verify one committed JSON artifact."""
        row = self.store.committed_output(run_id, task_id, output_name)
        if row is None:
            raise KeyError((run_id, task_id, output_name))
        manifest_path = Path(row["manifest_path"])
        if not manifest_path.is_absolute():
            raise RuntimeError("committed manifest path is not absolute")
        manifest_path = manifest_path.resolve()
        self._require_runtime_path(manifest_path, "committed manifest")
        manifest = strict_copy(json.loads(
            manifest_path.read_text(encoding="utf-8")))
        required = {
            "schema", "artifact_id", "recipe_id", "media_type",
            "content_sha256", "size_bytes", "object_path",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise RuntimeError("committed artifact manifest has invalid fields")
        if (manifest["schema"] != "stage1-artifact-manifest-v1"
                or manifest["media_type"] != "application/json"):
            raise RuntimeError("committed artifact manifest has unsupported schema")
        if manifest["content_sha256"] != row["content_sha256"]:
            raise RuntimeError("catalog and manifest content identities disagree")
        object_raw = Path(manifest["object_path"])
        object_path = (object_raw if object_raw.is_absolute()
                       else self.runtime_root / object_raw).resolve()
        self._require_runtime_path(object_path, "committed object")
        payload = object_path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != manifest["content_sha256"]:
            raise RuntimeError("committed artifact checksum mismatch")
        if len(payload) != manifest["size_bytes"]:
            raise RuntimeError("committed artifact size mismatch")
        return strict_copy(json.loads(payload.decode("utf-8")))

    def close(self) -> None:
        """Release controller ownership without killing durable attempts."""
        if self._closed:
            return
        self._closed = True
        try:
            try:
                self.provider.close()
            finally:
                self.store.stop_controller_session(self.controller_id)
        finally:
            self._lock.close()

    def __enter__(self) -> "WorkflowController":
        return self

    def __exit__(self, _type: object, _value: object,
                 _traceback: object) -> None:
        self.close()

    def _dispatch(self, run_id: str, task: BoundTask) -> None:
        try:
            preflight_request(task.resources, self.site)
            if operation_component(
                    task.component.operation_key) != task.component:
                raise ValueError(
                    "component is not an exact closed-registry binding")
        except (ValueError, RuntimeError) as exc:
            self.store.fail_ready_task_preflight(
                run_id, task.task_id, str(exc))
            return
        binding_id = deployment_id(
            self.store.load_graph_for_run(run_id).plan_id,
            self.site,
            self.provider.name,
        )
        # Use only generated hashes in filesystem paths; user-supplied run IDs
        # and task keys can never become traversal components.
        stage_key = strict_hash({
            "run_id": run_id,
            "task_id": task.task_id,
            "nonce": uuid.uuid4().hex,
        })
        stage_dir = self.runtime_root / "staging" / stage_key[:2] / stage_key
        spec = self.store.create_attempt(
            run_id,
            task,
            binding_id,
            self.provider.name,
            str(stage_dir),
            self.controller_epoch,
        )
        self.store.mark_submitting(spec.attempt_id)
        self._submit_persisted_spec(spec)

    def _submit_persisted_spec(self, spec: AttemptSpec) -> None:
        """Submit one already-persisted intent and attach its durable handle."""
        try:
            handle = self.provider.submit(spec)
        except SubmissionOutcomeUnknown as exc:
            self.store.mark_submission_unknown(spec.attempt_id, str(exc))
        except BaseException as exc:
            # The provider may have crossed Popen before raising. Preserve the
            # stable token first; process-level interrupts are then re-raised.
            self.store.mark_submission_unknown(
                spec.attempt_id,
                f"{type(exc).__name__}: {exc}",
            )
            if not isinstance(exc, Exception):
                raise
        else:
            self.store.attach_handle(spec.attempt_id, handle)

    def _reconcile_record(self, record: AttemptRecord) -> None:
        # CREATED cannot have crossed the external boundary: submission intent
        # is persisted in the next transaction. Recover this controller-only
        # crash window before observing provider state.
        if record.state is AttemptState.CREATED:
            self.store.mark_submitting(record.spec.attempt_id)
            self._submit_persisted_spec(record.spec)
            return

        # The local provider conditionally publishes attempt.json before
        # Popen. Its absence proves that a SUBMITTING transaction never crossed
        # the process boundary, so resuming this same stable-token submission
        # is safe and is not a blind duplicate submission.
        attempt_receipt = (
            Path(record.spec.stage_dir) / "supervisor" / "attempt.json")
        if (record.handle is None
                and record.state in {
                    AttemptState.SUBMITTING,
                    AttemptState.SUBMISSION_UNKNOWN,
                }
                and not attempt_receipt.exists()):
            self._submit_persisted_spec(record.spec)
            return

        value = record.handle if record.handle is not None else record.spec
        observation = self.provider.reconcile(value)
        if observation.recovered_handle is not None:
            # A fast attempt may already be terminal while the database still
            # says SUBMISSION_UNKNOWN. Recover SUBMITTED before applying its
            # terminal observation.
            self.store.attach_handle(
                observation.attempt_id, observation.recovered_handle)
        current = self.store.attempt_record(record.spec.attempt_id).state
        if observation.state is current:
            return
        if (current is AttemptState.SUBMITTING
                and observation.state is AttemptState.LOST):
            # A persisted provider receipt with no recoverable process proves
            # the submission outcome only after the crash-window state has
            # first been made explicit.  The store deliberately forbids the
            # ambiguous direct SUBMITTING -> LOST transition.
            self.store.mark_submission_unknown(
                record.spec.attempt_id,
                observation.error or "local submission outcome was unknown",
            )
        self.store.apply_observation(observation)

    def _stop_cancelled_attempt(self, record: AttemptRecord, *,
                                error: str) -> None:
        handle = record.handle
        if handle is None:
            observation = self.provider.reconcile(record.spec)
            if observation.recovered_handle is not None:
                self.store.attach_handle(
                    observation.attempt_id, observation.recovered_handle)
                handle = observation.recovered_handle
            current = self.store.attempt_record(record.spec.attempt_id).state
            # Absence of attempt.json is stronger than an ordinary UNKNOWN:
            # the local provider publishes it before Popen, so no process can
            # exist for this token. Persist LOST to avoid an immortal active
            # attempt after its task has already been fenced and cancelled.
            attempt_receipt = (
                Path(record.spec.stage_dir) / "supervisor" / "attempt.json")
            if (current is AttemptState.SUBMISSION_UNKNOWN
                    and observation.state is AttemptState.SUBMISSION_UNKNOWN
                    and not attempt_receipt.exists()):
                observation = ProviderObservation(
                    attempt_id=record.spec.attempt_id,
                    attempt_token=record.spec.attempt_token,
                    state=AttemptState.LOST,
                    observed_at=time.time(),
                    error=("cancelled before the provider submission receipt; "
                           "no local process was launched"),
                )
            if (observation.state is AttemptState.LOST
                    and observation.state is not current
                    and current is AttemptState.SUBMISSION_UNKNOWN):
                self.store.apply_observation(observation)
        if handle is not None:
            self.provider.cancel(handle)
        current_record = self.store.attempt_record(record.spec.attempt_id)
        if current_record.state is AttemptState.RESULT_READY:
            # request_cancel() already revoked the task fence. Let the artifact
            # boundary durably classify this completed-but-losing result as
            # SUPERSEDED; RESULT_READY cannot transition directly to CANCELLED.
            disposition = self.committer.process(current_record)
            if disposition is not CommitDisposition.STALE:
                raise RuntimeError(
                    "cancelled result did not lose its artifact commit fence")
            return
        self.store.mark_attempt_cancelled(record.spec.attempt_id, error)

    def _recover_created_attempts(self, run_id: str) -> None:
        # active_attempts() intentionally begins at SUBMITTING, so explicitly
        # repair a controller crash between create_attempt and mark_submitting.
        with self.store.connect() as con:
            attempt_ids = [row[0] for row in con.execute(
                "SELECT attempt_id FROM attempts WHERE run_id=? AND state=? "
                "ORDER BY created_at,attempt_id",
                (run_id, AttemptState.CREATED.value),
            )]
        for attempt_id in attempt_ids:
            self._reconcile_record(self.store.attempt_record(attempt_id))

    def _global_active_attempt_count(self) -> int:
        active = tuple(state.value for state in (
            AttemptState.CREATED, AttemptState.SUBMITTING,
            AttemptState.SUBMISSION_UNKNOWN, AttemptState.SUBMITTED,
            AttemptState.RUNNING, AttemptState.RESULT_READY,
        ))
        placeholders = ",".join("?" for _ in active)
        with self.store.connect() as con:
            return int(con.execute(
                f"SELECT COUNT(*) FROM attempts WHERE state IN ({placeholders})",
                active,
            ).fetchone()[0])

    def _require_runtime_path(self, path: Path, label: str) -> None:
        try:
            path.relative_to(self.runtime_root)
        except ValueError as exc:
            raise RuntimeError(f"{label} escapes runtime root") from exc

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("WorkflowController is closed")


__all__ = ["CommitDisposition", "WorkflowController"]
