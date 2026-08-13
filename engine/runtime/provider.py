"""Supervised local-subprocess execution provider for Stage 1.

This provider is deliberately small.  The controller owns admission, retries,
fencing, validation, and artifact commit; this module owns only the local OS
process boundary.  A submitted attempt is represented by durable files below
its attempt-scoped staging directory, so a reconstructed controller can
reconcile it without a ``Popen`` object.

Only operations in :mod:`engine.runtime.operations` are executable.  There is
no generic command, import, MPI, WRF, or batch-scheduler escape hatch.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from .identity import strict_canonical_json, strict_copy
from .operations import operation_component
from .site import current_private_site, preflight_request
from .types import (
    AttemptSpec,
    AttemptState,
    ExternalHandle,
    ProviderObservation,
    SiteSnapshot,
)


_ATTEMPT_FILE = "attempt.json"
_LAUNCH_RECEIPT = "launch_receipt.json"
_WORKER_RECEIPT = "worker_receipt.json"
_RECOVERED_RECEIPT = "recovered_receipt.json"
_CANCEL_RECEIPT = "cancelled.json"
_RESULT_FILE = "result.json"
_ERROR_FILE = "error.json"


class LocalSubprocessProvider:
    """Launch and reconcile one-node, process-group-isolated attempts.

    ``root`` must be the already-validated node-local runtime root.  Every
    ``AttemptSpec.stage_dir`` must be inside it.  There is intentionally no
    semaphore here: Stage 1's controller enforces ``max_inflight=1`` and later
    scheduling stages may replace that policy without changing this provider.
    """

    name = "stage1-local-subprocess"

    def __init__(self, root: Path | str, *,
                 site: SiteSnapshot | None = None,
                 python_executable: Path | str | None = None,
                 cancellation_grace_s: float = 1.0,
                 submission_grace_s: float = 0.25) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_absolute() or not self.root.is_dir():
            raise ValueError("provider root must be an existing absolute directory")
        self.site = site or current_private_site()
        self.python_executable = str(python_executable or sys.executable)
        self.cancellation_grace_s = float(cancellation_grace_s)
        self.submission_grace_s = float(submission_grace_s)
        if self.cancellation_grace_s < 0 or self.submission_grace_s < 0:
            raise ValueError("provider grace periods cannot be negative")
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def execution_sites(self) -> tuple[SiteSnapshot, ...]:
        return (self.site,)

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        """Launch a closed-registry worker and return its durable handle.

        The controller must persist ``SUBMITTING`` before calling this method.
        If a matching immutable attempt receipt already exists, this method
        does not launch again: it either reconstructs the known handle or
        raises ``SubmissionOutcomeUnknown`` for controller reconciliation.
        """
        self._validate_spec(spec)
        stage = self._stage_dir(spec.stage_dir)
        supervisor = stage / "supervisor"
        supervisor.mkdir(parents=True, exist_ok=True)
        attempt_path = supervisor / _ATTEMPT_FILE
        encoded_spec = spec.to_dict()

        created = _write_once_json(attempt_path, encoded_spec)
        if not created:
            existing = _read_json(attempt_path)
            if existing != strict_copy(encoded_spec):
                raise RuntimeError("attempt receipt conflicts with immutable spec")
            recovered = self._handle_from_receipts(spec, supervisor)
            if recovered is not None:
                return recovered
            terminal = self._terminal_observation(spec, stage)
            if terminal is not None:
                raise SubmissionOutcomeUnknown(
                    f"attempt {spec.attempt_id} already reached "
                    f"{terminal.state.value}; reconcile it instead of resubmitting")
            raise SubmissionOutcomeUnknown(
                f"submission receipt exists for {spec.attempt_id}, but no "
                "external handle is yet recoverable")

        stdout_path = supervisor / "stdout.log"
        stderr_path = supervisor / "stderr.log"
        stdout = stdout_path.open("ab", buffering=0)
        stderr = stderr_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                [
                    self.python_executable,
                    "-m", "engine.runtime.worker",
                    "--runtime-root", str(self.root),
                    "--stage-dir", str(stage),
                    "--attempt-token", spec.attempt_token,
                ],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=Path(__file__).resolve().parents[2],
                start_new_session=True,
                close_fds=True,
                env=_worker_environment(spec),
            )
        except BaseException:
            # The immutable attempt receipt is intentionally retained.  A
            # controller must reconcile the token rather than blindly submit.
            raise
        finally:
            stdout.close()
            stderr.close()

        start_ticks = _process_start_ticks(process.pid)
        launch = {
            "schema": "stage1-local-launch-receipt-v1",
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "pid": process.pid,
            "pgid": process.pid,
            "start_ticks": start_ticks,
            "launched_at": time.time(),
        }
        _write_once_json(supervisor / _LAUNCH_RECEIPT, launch)
        handle = self._handle(spec, stage, launch)
        with self._lock:
            self._children[spec.attempt_id] = process
        threading.Thread(
            target=self._reap_child,
            args=(spec.attempt_id, process),
            name=f"stage1-reap-{spec.attempt_id[:8]}",
            daemon=True,
        ).start()
        return handle

    def reconcile(self, value: ExternalHandle | AttemptSpec
                  ) -> ProviderObservation:
        """Observe a known handle or recover an unresolved stable token."""
        if isinstance(value, AttemptSpec):
            return self.reconcile_token(value)
        handle = value
        if handle.provider != self.name:
            raise ValueError(f"handle belongs to provider {handle.provider!r}")
        stage_raw = handle.metadata.get("stage_dir")
        if not isinstance(stage_raw, str):
            raise ValueError("local handle has no stage_dir")
        stage = self._stage_dir(stage_raw)
        spec = self._load_spec(stage)
        self._validate_handle(spec, handle, stage)
        terminal = self._terminal_observation(spec, stage)
        if terminal is not None:
            return terminal

        receipt = self._best_receipt(stage / "supervisor")
        if receipt is not None and _receipt_process_is_live(receipt):
            recovered = self._handle(spec, stage, receipt)
            return self._observation(
                spec, AttemptState.RUNNING, recovered_handle=recovered)
        return self._observation(
            spec, AttemptState.LOST,
            error="recorded local process is no longer live and no terminal marker exists",
            exit_code=self._known_exit_code(spec.attempt_id),
        )

    def reconcile_token(self, spec: AttemptSpec) -> ProviderObservation:
        """Reconcile ``SUBMITTING``/``SUBMISSION_UNKNOWN`` without a handle.

        The stable token is present in worker argv and in the worker-authored
        receipt.  This closes the parent-controller crash window between
        ``Popen`` and persistence of the returned external handle.
        """
        self._validate_spec(spec)
        stage = self._stage_dir(spec.stage_dir)
        attempt_path = stage / "supervisor" / _ATTEMPT_FILE
        if not attempt_path.exists():
            return self._observation(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="submission intent exists in the controller but provider receipt is absent")
        recorded = _read_json(attempt_path)
        if recorded != strict_copy(spec.to_dict()):
            return self._observation(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="provider attempt receipt conflicts with controller spec")

        receipt = self._best_receipt(stage / "supervisor")
        recovered = (self._handle(spec, stage, receipt)
                     if receipt is not None else None)
        terminal = self._terminal_observation(
            spec, stage, recovered_handle=recovered)
        if terminal is not None:
            return terminal
        if receipt is not None and _receipt_process_is_live(receipt):
            return self._observation(
                spec, AttemptState.RUNNING, recovered_handle=recovered)

        matches = _find_worker_processes(spec.attempt_token)
        if len(matches) == 1:
            pid, start_ticks = matches[0]
            recovered_receipt = {
                "schema": "stage1-local-recovered-receipt-v1",
                "attempt_id": spec.attempt_id,
                "attempt_token": spec.attempt_token,
                "pid": pid,
                "pgid": pid,
                "start_ticks": start_ticks,
                "recovered_at": time.time(),
            }
            _write_once_json(
                stage / "supervisor" / _RECOVERED_RECEIPT,
                recovered_receipt,
            )
            recovered = self._handle(spec, stage, recovered_receipt)
            return self._observation(
                spec, AttemptState.RUNNING, recovered_handle=recovered)
        if len(matches) > 1:
            return self._observation(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="multiple local processes advertise the same stable attempt token")

        # Use the provider-side receipt time, not logical attempt creation: a
        # READY task may legitimately wait before dispatch.
        age = max(0.0, time.time() - attempt_path.stat().st_mtime)
        if age < self.submission_grace_s:
            return self._observation(
                spec, AttemptState.SUBMISSION_UNKNOWN,
                error="local submission has no queryable receipt yet")
        return self._observation(
            spec, AttemptState.LOST,
            error="no local process or terminal marker exists for stable attempt token",
            exit_code=self._known_exit_code(spec.attempt_id),
        )

    def reconcile_many(self, values: Iterable[ExternalHandle | AttemptSpec]
                       ) -> list[ProviderObservation]:
        return [self.reconcile(value) for value in values]

    def cancel(self, handle: ExternalHandle, *, grace_s: float | None = None
               ) -> bool:
        """Terminate the exact process group represented by ``handle``.

        PID reuse is guarded by Linux process start ticks.  A result marker
        wins a completion/cancellation race; the controller's already-revoked
        fence still prevents that late result from being committed.
        """
        if handle.provider != self.name:
            raise ValueError(f"handle belongs to provider {handle.provider!r}")
        stage_raw = handle.metadata.get("stage_dir")
        if not isinstance(stage_raw, str):
            raise ValueError("local handle has no stage_dir")
        stage = self._stage_dir(stage_raw)
        spec = self._load_spec(stage)
        self._validate_handle(spec, handle, stage)
        if (stage / _RESULT_FILE).exists():
            return False

        receipt = self._best_receipt(stage / "supervisor")
        if receipt is None or not _receipt_process_is_live(receipt):
            return False
        self._handle(spec, stage, receipt)  # validate receipt identity
        pid = int(receipt["pid"])
        pgid = int(receipt.get("pgid", pid))
        if pgid != pid:
            raise RuntimeError("refusing to signal an unexpected process group")
        try:
            if os.getpgid(pid) != pgid:
                raise RuntimeError("local process is no longer in its recorded group")
        except ProcessLookupError:
            return False

        os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + (
            self.cancellation_grace_s if grace_s is None else max(0.0, grace_s))
        while time.monotonic() < deadline and _receipt_process_is_live(receipt):
            if (stage / _RESULT_FILE).exists():
                return False
            time.sleep(0.01)
        if _receipt_process_is_live(receipt):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            kill_deadline = time.monotonic() + 1.0
            while (time.monotonic() < kill_deadline
                   and _receipt_process_is_live(receipt)):
                time.sleep(0.01)
        if _receipt_process_is_live(receipt):
            raise RuntimeError("local process group did not terminate")
        if (stage / _RESULT_FILE).exists():
            return False
        _write_once_json(stage / "supervisor" / _CANCEL_RECEIPT, {
            "schema": "stage1-local-cancellation-v1",
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "pid": pid,
            "cancelled_at": time.time(),
        })
        return True

    def close(self) -> None:
        """Drop this controller view without killing reconcilable work."""

    def _validate_spec(self, spec: AttemptSpec) -> None:
        if spec.provider != self.name:
            raise ValueError(
                f"attempt requests provider {spec.provider!r}, expected {self.name!r}")
        preflight_request(spec.task.resources, self.site)
        expected = operation_component(spec.task.component.operation_key)
        if expected != spec.task.component:
            raise ValueError("attempt component is not an exact closed-registry binding")
        if not spec.attempt_id or not spec.attempt_token:
            raise ValueError("attempt identity cannot be empty")
        self._stage_dir(spec.stage_dir)

    def _stage_dir(self, raw: str | Path) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError("attempt stage_dir must be absolute")
        resolved = path.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("attempt stage_dir escapes the runtime root") from exc
        return resolved

    def _load_spec(self, stage: Path) -> AttemptSpec:
        raw = _read_json(stage / "supervisor" / _ATTEMPT_FILE)
        if raw is None:
            raise RuntimeError("attempt receipt is missing or invalid")
        spec = AttemptSpec.from_dict(raw)
        self._validate_spec(spec)
        if self._stage_dir(spec.stage_dir) != stage:
            raise RuntimeError("attempt receipt names a different staging directory")
        return spec

    def _validate_handle(self, spec: AttemptSpec, handle: ExternalHandle,
                         stage: Path) -> None:
        if (handle.attempt_id != spec.attempt_id
                or handle.attempt_token != spec.attempt_token):
            raise RuntimeError("external handle does not match attempt receipt")
        if Path(str(handle.metadata.get("stage_dir", ""))).resolve() != stage:
            raise RuntimeError("external handle staging path mismatch")

    def _best_receipt(self, supervisor: Path) -> dict | None:
        return (_read_json(supervisor / _WORKER_RECEIPT)
                or _read_json(supervisor / _RECOVERED_RECEIPT)
                or _read_json(supervisor / _LAUNCH_RECEIPT))

    def _handle_from_receipts(self, spec: AttemptSpec,
                              supervisor: Path) -> ExternalHandle | None:
        receipt = self._best_receipt(supervisor)
        if receipt is None:
            return None
        return self._handle(spec, supervisor.parent, receipt)

    def _handle(self, spec: AttemptSpec, stage: Path,
                receipt: dict) -> ExternalHandle:
        if (receipt.get("attempt_id") != spec.attempt_id
                or receipt.get("attempt_token") != spec.attempt_token):
            raise RuntimeError("process receipt does not match attempt identity")
        return ExternalHandle(
            provider=self.name,
            external_id=str(receipt["pid"]),
            attempt_id=spec.attempt_id,
            attempt_token=spec.attempt_token,
            metadata={
                "stage_dir": str(stage),
                "pid": int(receipt["pid"]),
                "pgid": int(receipt.get("pgid", receipt["pid"])),
                "start_ticks": receipt.get("start_ticks"),
                "site_snapshot_id": self.site.snapshot_id,
            },
        )

    def _terminal_observation(
            self, spec: AttemptSpec, stage: Path, *,
            recovered_handle: ExternalHandle | None = None,
    ) -> ProviderObservation | None:
        result_path = stage / _RESULT_FILE
        if result_path.exists():
            result = _read_json(result_path)
            if _valid_result_manifest(result, spec, stage):
                return self._observation(
                    spec, AttemptState.RESULT_READY,
                    result_manifest_path=str(result_path), exit_code=0,
                    recovered_handle=recovered_handle)
            return self._observation(
                spec, AttemptState.FAILED,
                error="worker result marker is invalid", exit_code=1,
                recovered_handle=recovered_handle)
        error_path = stage / _ERROR_FILE
        if error_path.exists():
            error = _read_json(error_path) or {}
            if (error.get("attempt_id") != spec.attempt_id
                    or error.get("attempt_token") != spec.attempt_token):
                message = "worker error marker identity mismatch"
            else:
                message = str(error.get("error", "worker failed"))
            return self._observation(
                spec, AttemptState.FAILED, error=message,
                exit_code=self._known_exit_code(spec.attempt_id, default=1),
                recovered_handle=recovered_handle)
        if (stage / "supervisor" / _CANCEL_RECEIPT).exists():
            return self._observation(
                spec, AttemptState.CANCELLED, error="cancelled",
                exit_code=self._known_exit_code(spec.attempt_id),
                recovered_handle=recovered_handle)
        return None

    def _observation(self, spec: AttemptSpec, state: AttemptState, *,
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

    def _known_exit_code(self, attempt_id: str,
                         default: int | None = None) -> int | None:
        with self._lock:
            process = self._children.get(attempt_id)
        if process is None:
            return default
        value = process.poll()
        return default if value is None else value

    def _reap_child(self, attempt_id: str,
                    process: subprocess.Popen[bytes]) -> None:
        process.wait()
        # Keep the completed Popen long enough for exit-code reporting.  It
        # holds no pipe descriptors and does not affect admission.
        with self._lock:
            self._children[attempt_id] = process


class SubmissionOutcomeUnknown(RuntimeError):
    """The stable token must be reconciled before any new submission."""


def _worker_environment(spec: AttemptSpec) -> dict[str, str]:
    env = dict(os.environ)
    threads = str(max(1, spec.task.resources.cpu_cores))
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                 "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = threads
    return env


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = strict_copy(raw)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
        return None


def _write_once_json(path: Path, value: dict) -> bool:
    """Create and fsync an immutable JSON receipt.

    Returns ``False`` when the path already contains exactly the same value and
    raises on a conflicting existing receipt.
    """
    encoded = (strict_canonical_json(value) + "\n").encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"temporary receipt collision at {temporary}")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Same-directory hard-link publication is atomic and conditional:
            # readers never observe a partially written marker, and an
            # existing immutable marker is never overwritten.
            os.link(temporary, path)
            created = True
        except FileExistsError:
            existing = _read_json(path)
            if existing != strict_copy(value):
                raise RuntimeError(f"immutable receipt conflict at {path}")
            created = False
        temporary.unlink(missing_ok=True)
        _fsync_dir(path.parent)
        return created
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _process_start_ticks(pid: int) -> int | None:
    try:
        rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(rest[19])
    except (FileNotFoundError, PermissionError, IndexError, ValueError):
        return None


def _receipt_process_is_live(receipt: dict) -> bool:
    try:
        pid = int(receipt["pid"])
        expected = receipt.get("start_ticks")
        expected = int(expected) if expected is not None else None
    except (KeyError, TypeError, ValueError):
        return False
    if expected is None:
        # Never signal or reattach using only a PID; it may have been reused.
        return False
    observed = _process_start_ticks(pid)
    if observed != expected:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, PermissionError, IndexError):
        return False


def _find_worker_processes(attempt_token: str) -> list[tuple[int, int]]:
    matches: list[tuple[int, int]] = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
            decoded = [item.decode("utf-8") for item in argv if item]
        except (FileNotFoundError, PermissionError, ProcessLookupError,
                UnicodeError, OSError):
            continue
        if ("engine.runtime.worker" not in decoded
                or "--attempt-token" not in decoded
                or attempt_token not in decoded):
            continue
        pid = int(proc.name)
        start_ticks = _process_start_ticks(pid)
        if start_ticks is not None:
            matches.append((pid, start_ticks))
    return sorted(matches)


def _valid_result_manifest(value: dict | None, spec: AttemptSpec,
                           stage: Path) -> bool:
    if not isinstance(value, dict):
        return False
    if (value.get("schema") != "stage1-attempt-result-v1"
            or value.get("attempt_id") != spec.attempt_id
            or value.get("attempt_token") != spec.attempt_token):
        return False
    outputs = value.get("outputs")
    expected = {recipe.output_name for recipe in spec.task.outputs}
    if not isinstance(outputs, dict) or set(outputs) != expected:
        return False
    for port, descriptor in outputs.items():
        if not isinstance(descriptor, dict) or set(descriptor) != {"path"}:
            return False
        relative = Path(str(descriptor["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            return False
        payload = (stage / relative).resolve()
        try:
            payload.relative_to(stage)
        except ValueError:
            return False
        if not payload.is_file() or relative != Path("outputs") / port / "payload.json":
            return False
    return True
