"""Candidate execution providers used only by the Stage-0A comparison."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import TimeoutError
from pathlib import Path
from typing import Any

from .conformance import (
    AttemptSpec,
    AttemptState,
    ExternalHandle,
    ProviderResult,
    execute_operation,
)


class ThinLocalSubprocessProvider:
    """Disposable reference provider with honest local restart semantics.

    Each attempt has a private directory in `/tmp`, a PID/start-time record,
    stdout/stderr files, and an atomic result marker. A reconstructed provider
    can reconcile a live PID or completed marker without retaining a Future.
    Stage 1 still needs a transactional state store, leases, retries, and the
    production ArtifactCommitter.
    """

    name = "thin-local-subprocess"

    def __init__(self, root: Path, *, max_workers: int = 2) -> None:
        self.root = root.resolve()
        if not str(self.root).startswith("/tmp/"):
            raise ValueError("Stage-0A subprocess state must be under /tmp")
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, int(max_workers))
        self._slots = threading.BoundedSemaphore(self.max_workers)
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def _attempt_dir(self, attempt_id: str) -> Path:
        return self.root / attempt_id

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        attempt_dir = self._attempt_dir(spec.attempt_id)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        (attempt_dir / "attempt.json").write_text(
            __import__("json").dumps(spec.to_dict(), sort_keys=True))
        if not self._slots.acquire(timeout=spec.timeout_s):
            raise TimeoutError("local provider admission timeout")
        stdout = (attempt_dir / "stdout.log").open("wb")
        stderr = (attempt_dir / "stderr.log").open("wb")
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "stage0a.worker", str(attempt_dir)],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                cwd=Path(__file__).resolve().parents[1],
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            self._slots.release()
            stdout.close()
            stderr.close()
            raise
        finally:
            # Popen has duplicated the descriptors.
            stdout.close()
            stderr.close()
        start_ticks = _process_start_ticks(process.pid)
        (attempt_dir / "process.json").write_text(__import__("json").dumps({
            "pid": process.pid,
            "start_ticks": start_ticks,
        }, sort_keys=True))
        with self._lock:
            self._processes[spec.attempt_id] = process
        threading.Thread(
            target=self._release_when_done,
            args=(spec.attempt_id, process), daemon=True).start()
        return ExternalHandle(
            provider=self.name,
            external_id=str(process.pid),
            attempt_id=spec.attempt_id,
            attempt_token=(
                f"{spec.task_id}:{spec.attempt_id}:{spec.fencing_token}"),
            metadata={
                "resources": spec.to_dict()["resources"],
                "state_dir": str(attempt_dir),
                "pid_start_ticks": start_ticks,
            },
        )

    def _release_when_done(self, attempt_id: str,
                           process: subprocess.Popen) -> None:
        process.wait()
        with self._lock:
            if self._processes.pop(attempt_id, None) is process:
                self._slots.release()

    def reconcile(self, handle: ExternalHandle) -> AttemptState:
        attempt_dir = self._attempt_dir(handle.attempt_id)
        if (attempt_dir / "cancelled").exists():
            return AttemptState.CANCELLED
        result_path = attempt_dir / "result.json"
        if result_path.exists():
            result = _read_result(result_path)
            return result.state
        error_path = attempt_dir / "error.json"
        if error_path.exists():
            return AttemptState.FAILED
        record = _read_json(attempt_dir / "process.json")
        if record and _same_live_process(
                int(record["pid"]), record.get("start_ticks")):
            return AttemptState.RUNNING
        return AttemptState.LOST

    def result(self, handle: ExternalHandle,
               timeout_s: float) -> ProviderResult:
        deadline = time.monotonic() + timeout_s
        attempt_dir = self._attempt_dir(handle.attempt_id)
        while time.monotonic() < deadline:
            state = self.reconcile(handle)
            if state is AttemptState.SUCCEEDED:
                return _read_result(attempt_dir / "result.json")
            if state is AttemptState.FAILED:
                raw = _read_json(attempt_dir / "error.json") or {}
                return ProviderResult(state, error=raw.get("error", "failed"))
            if state in {AttemptState.CANCELLED, AttemptState.LOST}:
                return ProviderResult(state, error=state.value.lower())
            time.sleep(0.01)
        return ProviderResult(AttemptState.UNKNOWN, error="result timeout")

    def cancel(self, handle: ExternalHandle) -> bool:
        attempt_dir = self._attempt_dir(handle.attempt_id)
        record = _read_json(attempt_dir / "process.json")
        if not record:
            return False
        pid = int(record["pid"])
        if not _same_live_process(pid, record.get("start_ticks")):
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and _same_live_process(
                pid, record.get("start_ticks")):
            time.sleep(0.01)
        if _same_live_process(pid, record.get("start_ticks")):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        (attempt_dir / "cancelled").touch(exist_ok=True)
        return True

    def restart_view(self) -> "ThinLocalSubprocessProvider":
        # A new object models a restarted controller: it has no Popen/Future
        # references and relies only on persisted PID/result records.
        return ThinLocalSubprocessProvider(
            self.root, max_workers=self.max_workers)

    def close(self) -> None:
        # Closing a controller view must not kill externally reconcilable work.
        pass


class _FutureProvider:
    """Common in-memory adapter for optional framework comparisons."""

    name = "future-provider"

    def __init__(self) -> None:
        self._futures: dict[str, Any] = {}

    def _future_handle(self, spec: AttemptSpec, external_id: str) -> ExternalHandle:
        return ExternalHandle(
            provider=self.name,
            external_id=external_id,
            attempt_id=spec.attempt_id,
            attempt_token=f"{spec.task_id}:{spec.attempt_id}:{spec.fencing_token}",
            metadata={"resources": spec.to_dict()["resources"]},
        )

    def reconcile(self, handle: ExternalHandle) -> AttemptState:
        future = self._futures.get(handle.external_id)
        if future is None:
            return AttemptState.LOST
        if future.cancelled():
            return AttemptState.CANCELLED
        if future.done():
            try:
                future.result()
                return AttemptState.SUCCEEDED
            except BaseException:
                return AttemptState.FAILED
        return AttemptState.RUNNING

    def cancel(self, handle: ExternalHandle) -> bool:
        future = self._futures.get(handle.external_id)
        if future is None:
            return False
        try:
            outcome = future.cancel()
        except (NotImplementedError, RuntimeError):
            return False
        # concurrent.futures returns bool; Dask may return None after issuing
        # the request, so reconcile once before deciding.
        if outcome is not None:
            return bool(outcome)
        return self.reconcile(handle) is AttemptState.CANCELLED

    def restart_view(self):
        # These adapters have no durable scheduler/handle reconstruction in the
        # Stage-0A configuration. Be explicit by returning an empty view.
        return type(self)._empty_restart_view()

    def close(self) -> None:
        pass


class DaskDistributedProvider(_FutureProvider):
    name = "dask-distributed-local"

    def __init__(self, *, n_workers: int = 2) -> None:
        super().__init__()
        from distributed import Client, LocalCluster
        self._cluster = LocalCluster(
            n_workers=n_workers, threads_per_worker=1, processes=True,
            dashboard_address=None)
        self._client = Client(self._cluster)

    @classmethod
    def _empty_restart_view(cls):
        obj = object.__new__(cls)
        _FutureProvider.__init__(obj)
        obj._cluster = None
        obj._client = None
        return obj

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        future = self._client.submit(
            execute_operation, spec.operation, spec.payload,
            key=spec.attempt_id, pure=False)
        self._futures[spec.attempt_id] = future
        return self._future_handle(spec, spec.attempt_id)

    def result(self, handle: ExternalHandle,
               timeout_s: float) -> ProviderResult:
        future = self._futures.get(handle.external_id)
        if future is None:
            return ProviderResult(AttemptState.LOST, error="not reattachable")
        try:
            return ProviderResult(
                AttemptState.SUCCEEDED, output=future.result(timeout=timeout_s))
        except BaseException as exc:
            state = self.reconcile(handle)
            return ProviderResult(state, error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if getattr(self, "_client", None) is not None:
            self._client.close()
        if getattr(self, "_cluster", None) is not None:
            self._cluster.close()


class ParslProvider(_FutureProvider):
    name = "parsl-local"

    def __init__(self, *, max_threads: int = 2) -> None:
        super().__init__()
        import parsl
        from parsl.config import Config
        from parsl.executors.threads import ThreadPoolExecutor
        self._parsl = parsl
        self._dfk = parsl.load(Config(
            executors=[ThreadPoolExecutor(
                label=f"stage0a-{uuid.uuid4().hex[:8]}",
                max_threads=max_threads)],
            retries=0,
            run_dir=tempfile.mkdtemp(
                prefix="nasa-stage0a-parsl-", dir="/tmp"),
        ))

        @parsl.python_app(cache=False)
        def _app(operation, payload):
            return execute_operation(operation, payload)
        self._app = _app

    @classmethod
    def _empty_restart_view(cls):
        obj = object.__new__(cls)
        _FutureProvider.__init__(obj)
        obj._parsl = None
        obj._dfk = None
        obj._app = None
        return obj

    def submit(self, spec: AttemptSpec) -> ExternalHandle:
        future = self._app(spec.operation, spec.payload)
        external_id = str(future.tid)
        self._futures[external_id] = future
        return self._future_handle(spec, external_id)

    def result(self, handle: ExternalHandle,
               timeout_s: float) -> ProviderResult:
        future = self._futures.get(handle.external_id)
        if future is None:
            return ProviderResult(AttemptState.LOST, error="not reattachable")
        try:
            return ProviderResult(
                AttemptState.SUCCEEDED, output=future.result(timeout=timeout_s))
        except BaseException as exc:
            state = self.reconcile(handle)
            return ProviderResult(state, error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if getattr(self, "_dfk", None) is not None:
            self._dfk.cleanup()
            self._parsl.clear()


def candidate_availability() -> dict[str, dict[str, Any]]:
    candidates = {
        "thin-local-subprocess": (None, None),
        "dask-distributed-local": ("distributed", "distributed"),
        "parsl-local": ("parsl", "parsl"),
    }
    result: dict[str, dict[str, Any]] = {}
    for name, (module, distribution) in candidates.items():
        if module is None:
            result[name] = {"available": True, "version": "project-spike-v1"}
            continue
        available = importlib.util.find_spec(module) is not None
        version = None
        if available:
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = "unknown"
        result[name] = {"available": available, "version": version}
    return result


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return __import__("json").loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def _read_result(path: Path) -> ProviderResult:
    raw = _read_json(path)
    if raw is None:
        return ProviderResult(AttemptState.LOST, error="missing result")
    return ProviderResult(
        state=AttemptState(raw["state"]),
        output=raw.get("output"),
        error=raw.get("error"),
    )


def _process_start_ticks(pid: int) -> int | None:
    try:
        # Field 22 follows the comm, which may contain spaces/parentheses.
        rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(rest[19])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def _same_live_process(pid: int, start_ticks: int | None) -> bool:
    observed = _process_start_ticks(pid)
    if observed is None or (start_ticks is not None and observed != start_ticks):
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, IndexError):
        return False
