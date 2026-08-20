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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .artifacts import ArtifactCommitter, CommitDisposition
from .identity import strict_copy, strict_hash, strict_json_loads
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


@dataclass(frozen=True)
class _ReservationClaim:
    """Capacity held before the durable attempt/fence identity is minted."""

    owner_id: str | None
    site_id: str | None
    envelope: object | None
    started_at: float | None


@dataclass(frozen=True)
class _LiveReservation:
    """One ledger claim keyed by its durable attempt identity."""

    run_id: str
    task_id: str
    site_id: str
    envelope: object
    started_at: float | None


class WorkflowController:
    """Durable controller for an already-bound execution graph.

    The one-at-a-time default remains conservative.  Higher concurrency is
    available only through a capacity ledger whose logical capacities are
    checked against the provider's physical site snapshot.
    """

    def __init__(self, runtime_root: Path | str, *,
                 site: SiteSnapshot | None = None,
                 poll_interval_s: float = 0.02,
                 max_inflight: int = 1,
                 artifact_failpoint: Callable[[str], None] | None = None,
                 provider: LocalSubprocessProvider | None = None,
                 ledger: "ReservationLedger | None" = None,
                 site_id: str | None = None,
                 priority_policy: "PriorityPolicy | None" = None,
                 observations: "ObservationHistory | None" = None,
                 artifact_commit_observer: Callable[
                     ["WorkflowController", str], None] | None = None) -> None:
        root, fs_type = validate_runtime_root(Path(runtime_root))
        if (isinstance(max_inflight, bool) or not isinstance(max_inflight, int)
                or max_inflight < 1):
            raise ValueError("max_inflight must be a positive integer")
        if max_inflight > 1 and ledger is None:
            # Concurrency without a reservation ledger is exactly the
            # oversubscription the Stage-8 policy exists to prevent: the
            # controller would start N tasks knowing nothing about the node.
            raise ValueError(
                "max_inflight above 1 requires a ReservationLedger so "
                "concurrent attempts cannot oversubscribe the node")
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        self.runtime_root = root
        self.filesystem_type = fs_type
        self.site = site or current_private_site()
        self.poll_interval_s = poll_interval_s
        self.max_inflight = max_inflight
        self.ledger = ledger
        self.scheduling_site_id = site_id
        self._validate_ledger_site_binding()
        self.priority_policy = priority_policy
        self.observations = observations
        self.artifact_commit_observer = artifact_commit_observer
        # attempt_id -> claim. Attempt identity includes run and fence, so the
        # same scientific task may execute concurrently in independent runs.
        self._reserved: dict[str, _LiveReservation] = {}
        self._ready_since: dict[tuple[str, str], float] = {}
        self.controller_id = uuid.uuid4().hex
        self._closed = False
        self._lock = ControllerLock(root / "control" / "controller.lock")
        try:
            self.store = RuntimeStore(root / "control" / "runtime.sqlite3")
            self.controller_epoch = self.store.start_controller_session(
                self.controller_id)
            swept = self.store.sweep_terminal_attempt_reservations()
            if self.ledger is not None:
                for attempt_id in swept:
                    if self.ledger.reservation(attempt_id) is not None:
                        self.ledger.release(attempt_id)
            self._recover_resource_reservations()
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

    def _validate_ledger_site_binding(self) -> None:
        """Refuse logical capacity that the bound provider cannot supply.

        A reservation ledger is useful only if its accounting universe is no
        larger than the actual cpuset/memory/GPU allocation.  Multiple logical
        sites are safe on one local provider only when they explicitly share a
        single physical-host envelope; otherwise their independent capacities
        could double-count the same machine.
        """
        if self.ledger is None:
            if self.scheduling_site_id is not None:
                raise ValueError("site_id requires a ReservationLedger")
            return
        from scheduling import ResourceEnvelopeSpec

        physical = ResourceEnvelopeSpec(
            cpu_cores=len(self.site.cpuset),
            memory_mb=self.site.memory_mb,
            gpus=len(self.site.gpu_ids),
            # The current SiteSnapshot has no scratch-allocation field.  The
            # live controller consequently requests zero scratch.
            scratch_mb=0,
        )
        sites = self.ledger.sites
        if self.scheduling_site_id is not None:
            selected = (self.ledger.site(self.scheduling_site_id),)
        else:
            selected = sites
        for logical in selected:
            comparable = ResourceEnvelopeSpec(
                logical.capacity.cpu_cores,
                logical.capacity.memory_mb,
                logical.capacity.gpus,
                0,
            )
            if not comparable.fits_within(physical):
                raise ValueError(
                    f"ledger site {logical.site_id!r} exceeds the bound "
                    "provider site capacity")

        if self.scheduling_site_id is None and len(sites) > 1:
            host_ids = {site.host_id for site in sites}
            if None in host_ids or len(host_ids) != 1:
                raise ValueError(
                    "multiple logical scheduling sites on one provider must "
                    "declare one shared physical host capacity")
            host = sites[0].host_capacity
            assert host is not None
            comparable_host = ResourceEnvelopeSpec(
                host.cpu_cores, host.memory_mb, host.gpus, 0)
            if not comparable_host.fits_within(physical):
                raise ValueError(
                    "ledger shared-host capacity exceeds the bound provider "
                    "site capacity")

    def create_run(self, graph: BoundExecutionGraph, *,
                   run_id: str | None = None) -> str:
        """Persist an immutable bound graph and start a new run."""
        self._require_open()
        return self.store.create_run(graph, run_id=run_id)

    def tick(self, run_id: str) -> RunState:
        """Advance one nonblocking controller iteration."""
        self._require_open()
        self._prune_ready_since()
        current = self.store.run_state(run_id)
        if current is not RunState.RUNNING:
            self._observe_artifact_commits(run_id)
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

        self._release_finished_reservations()
        active = self._global_active_attempt_count()
        if active < self.max_inflight:
            ready = self.store.ready_tasks(run_id)
            for task in self._order_ready(run_id, ready):
                if active >= self.max_inflight:
                    break
                claim = self._reserve_for(run_id, task)
                if claim is None:
                    continue      # no capacity for this one yet; try the next
                before = self._global_active_attempt_count()
                try:
                    self._dispatch(run_id, task, claim)
                except BaseException:
                    self._release_claim(claim)
                    raise
                if self._global_active_attempt_count() > before:
                    active += 1
                else:
                    self._release_claim(claim)

        finalized = self.store.finalize_run_state(run_id)
        self._observe_artifact_commits(run_id)
        return finalized

    def _observe_artifact_commits(self, run_id: str) -> None:
        if self.artifact_commit_observer is not None:
            self.artifact_commit_observer(self, run_id)

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
        self._release_finished_reservations()
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
                self._release_finished_reservations()
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

    def _dispatch(
            self, run_id: str, task: BoundTask,
            claim: _ReservationClaim,
    ) -> AttemptSpec | None:
        try:
            preflight_request(task.resources, self.site)
            if operation_component(
                    task.component.operation_key) != task.component:
                raise ValueError(
                    "component is not an exact closed-registry binding")
        except (ValueError, RuntimeError) as exc:
            self.store.fail_ready_task_preflight(
                run_id, task.task_id, str(exc))
            return None
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
            reservation_site_id=claim.site_id,
            reservation_envelope=(
                claim.envelope.to_dict()
                if claim.envelope is not None else None),
        )
        if self.ledger is not None:
            assert claim.owner_id is not None
            assert claim.site_id is not None
            assert claim.envelope is not None
            self.ledger.rekey(claim.owner_id, spec.attempt_id)
            self._reserved[spec.attempt_id] = _LiveReservation(
                run_id=run_id,
                task_id=task.task_id,
                site_id=claim.site_id,
                envelope=claim.envelope,
                started_at=claim.started_at,
            )
        self.store.mark_submitting(spec.attempt_id)
        self._submit_persisted_spec(spec)
        return spec

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

    # -- Stage-8 policy bridge --------------------------------------------

    def _order_ready(self, run_id: str,
                     ready: "list[BoundTask]") -> "list[BoundTask]":
        """Order ready work by the Stage-8 policy when one was supplied.

        With no policy this is the Stage-1 order the store already returned,
        so existing behaviour is untouched.
        """
        if self.priority_policy is None or len(ready) < 2:
            return ready
        from scheduling import (ScheduledNode, critical_path_ranks,
                                order_ready_tasks)
        graph = self.store.load_graph_for_run(run_id)
        by_key = {task.key: task for task in graph.tasks}
        nodes = []
        for task in graph.tasks:
            declared = float(getattr(task.resources, "walltime_s", 1.0) or 1.0)
            estimate = declared
            if self.observations is not None:
                estimate = self.observations.duration_estimate(
                    task.key, declared).value
            nodes.append(ScheduledNode(
                task.task_id,
                estimate,
                tuple(by_key[binding.upstream_task].task_id
                      for binding in task.inputs),
            ))
        ranks = critical_path_ranks(nodes)
        now = time.monotonic()
        for task in ready:
            self._ready_since.setdefault((run_id, task.task_id), now)
        waiting = {task.task_id: max(now - self._ready_since[
            (run_id, task.task_id)], 0.0) for task in ready}
        by_id = {task.task_id: task for task in ready}
        return [by_id[key] for key in order_ready_tasks(
            [task.task_id for task in ready], ranks, waiting,
            self.priority_policy)]

    def _prune_ready_since(self) -> None:
        """Retain aging timestamps only while their durable task is READY."""
        if not self._ready_since:
            return
        ready = self.store.ready_task_identities()
        for identity in tuple(self._ready_since):
            if identity not in ready:
                self._ready_since.pop(identity, None)

    def _envelope_for(self, task: "BoundTask"):
        from scheduling import ResourceEnvelopeSpec
        request = task.resources
        return ResourceEnvelopeSpec(
            cpu_cores=max(1, int(getattr(request, "cpu_cores", 1))),
            memory_mb=max(1, int(getattr(request, "memory_mb", 1))),
            gpus=max(0, int(getattr(request, "gpus", 0))),
            scratch_mb=0)

    def _recover_resource_reservations(self) -> None:
        """Fence node capacity from every durable active attempt on restart."""
        if self.ledger is None:
            return
        from scheduling import (
            OversubscriptionError,
            ResourceEnvelopeSpec,
            best_fit_site,
        )

        added: list[str] = []
        try:
            for record in self.store.active_attempt_reservations():
                attempt_id = record.spec.attempt_id
                expected = self._envelope_for(record.spec.task)
                if record.released_at is not None:
                    raise RuntimeError(
                        "an active attempt has a released durable reservation")
                if record.envelope is None:
                    site_id = self.scheduling_site_id or best_fit_site(
                        self.ledger, expected)
                    if site_id is None:
                        raise RuntimeError(
                            "node capacity cannot cover a durable active attempt")
                    envelope = expected
                else:
                    if record.site_id is None:
                        raise RuntimeError(
                            "durable attempt reservation has no execution site")
                    site_id = record.site_id
                    envelope = ResourceEnvelopeSpec.from_dict(record.envelope)
                    if envelope != expected:
                        raise RuntimeError(
                            "durable attempt reservation disagrees with its "
                            "immutable task resource request")

                existing = self.ledger.reservation(attempt_id)
                if existing is None:
                    try:
                        self.ledger.reserve(attempt_id, site_id, envelope)
                    except (KeyError, OversubscriptionError, ValueError) as exc:
                        raise RuntimeError(
                            "node capacity cannot reconstruct durable active "
                            f"attempt {attempt_id}: {exc}") from exc
                    added.append(attempt_id)
                elif (existing.site_id != site_id
                      or existing.envelope != envelope):
                    raise RuntimeError(
                        "live ledger reservation conflicts with durable attempt")

                if record.envelope is None:
                    self.store.adopt_attempt_reservation(
                        attempt_id, site_id, envelope.to_dict())
                self._reserved[attempt_id] = _LiveReservation(
                    run_id=record.spec.run_id,
                    task_id=record.spec.task.task_id,
                    site_id=site_id,
                    envelope=envelope,
                    # A monotonic start timestamp cannot survive a process
                    # restart. Do not invent an underestimate for observations.
                    started_at=None,
                )
            if not self.ledger.invariant_holds():
                raise RuntimeError(
                    "reconstructed resource reservations exceed node capacity")
        except BaseException:
            for attempt_id in reversed(added):
                try:
                    self.ledger.release(attempt_id)
                except KeyError:
                    pass
            self._reserved.clear()
            raise

    def _reserve_for(
            self, run_id: str, task: "BoundTask",
    ) -> _ReservationClaim | None:
        """Claim capacity before dispatch, or decline to start this task."""
        if self.ledger is None:
            return _ReservationClaim(None, None, None, None)
        from scheduling import OversubscriptionError, best_fit_site
        envelope = self._envelope_for(task)
        site_id = self.scheduling_site_id or best_fit_site(
            self.ledger, envelope)
        if site_id is None:
            return None
        owner_id = "dispatch:" + strict_hash({
            "schema": "stage8r-pre-attempt-reservation-v1",
            "controller_epoch": self.controller_epoch,
            "run_id": run_id,
            "task_id": task.task_id,
            "nonce": uuid.uuid4().hex,
        })
        try:
            self.ledger.reserve(owner_id, site_id, envelope)
        except (KeyError, OversubscriptionError):
            return None
        return _ReservationClaim(
            owner_id, site_id, envelope, time.monotonic())

    def _release_claim(self, claim: _ReservationClaim) -> None:
        """Release only a pre-attempt claim that was never durably adopted."""
        if self.ledger is None or claim.owner_id is None:
            return
        try:
            self.ledger.release(claim.owner_id)
        except KeyError:
            # Successful dispatch rekeys it to attempt_id, where the normal
            # terminal-attempt release path owns it.
            pass

    def _release_finished_reservations(self) -> None:
        """Return capacity only after the owning durable attempt is terminal."""
        if self.ledger is None or not self._reserved:
            return
        active = {
            record.spec.attempt_id
            for record in self.store.active_attempt_reservations()
        }
        for attempt_id, reservation in list(self._reserved.items()):
            if attempt_id in active:
                continue
            self.ledger.release(attempt_id)
            self.store.release_attempt_reservation(attempt_id)
            self._reserved.pop(attempt_id, None)
            if reservation.started_at is not None:
                self._record_observation(
                    reservation.run_id,
                    reservation.task_id,
                    reservation.envelope,
                    reservation.started_at,
                )

    def _record_observation(self, run_id: str, task_id: str, envelope,
                            started: float) -> None:
        """Emit a measured observation for a finished task, if asked to.

        Duration is wall clock from reservation to release.  Peak memory is
        the worker's real ``ru_maxrss`` when the attempt reported one; when it
        did not, the reserved envelope is used unchanged rather than a
        fabricated figure, so the revision machinery never sees an invented
        measurement.
        """
        if self.observations is None:
            return
        from scheduling import (ObservationKind, ResourceEnvelopeSpec,
                                TaskObservation)
        row = next((item for item in self.store.task_rows(run_id)
                    if item["task_id"] == task_id), None)
        if row is None:
            return
        kind = (ObservationKind.COMPLETED if row["state"] == "SUCCEEDED"
                else ObservationKind.FAILED)
        measured = self._measured_peak(run_id, task_id)
        if measured is not None:
            envelope = ResourceEnvelopeSpec(
                cpu_cores=envelope.cpu_cores, memory_mb=measured,
                gpus=envelope.gpus, scratch_mb=envelope.scratch_mb)
        self.observations.record(TaskObservation(
            task_key=row["task_key"],
            duration_s=max(time.monotonic() - started, 0.0),
            peak=envelope, kind=kind))

    def _measured_peak(self, run_id: str, task_id: str) -> int | None:
        """The worker's reported peak RSS in MB, or None when unavailable."""
        stage_dir = self.store.latest_attempt_stage_dir(run_id, task_id)
        if stage_dir is None:
            return None
        result = Path(stage_dir) / "result.json"
        if not result.exists():
            return None
        try:
            payload = strict_json_loads(result.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw = payload.get("peak_memory_kb") if isinstance(payload, dict) else None
        if not isinstance(raw, int) or raw <= 0:
            return None
        return max(1, raw // 1024)

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
