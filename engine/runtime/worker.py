"""Closed-operation worker for the Stage-1 local subprocess provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .identity import strict_canonical_json, strict_copy
from .operations import execute_component, operation_component
from .types import AttemptSpec


_SAFE_PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--attempt-token", required=True)
    args = parser.parse_args(argv)

    runtime_root = args.runtime_root.resolve()
    stage = args.stage_dir.resolve()
    _require_within(stage, runtime_root, "stage directory")
    supervisor = stage / "supervisor"
    spec: AttemptSpec | None = None
    try:
        raw = _read_strict_json(supervisor / "attempt.json")
        spec = AttemptSpec.from_dict(raw)
        _verify_invocation(spec, args.attempt_token, stage)
        _claim_worker(supervisor, spec)

        inputs = _load_inputs(spec, runtime_root)
        outputs = execute_component(
            spec.task.component,
            strict_copy(spec.task.parameters),
            inputs,
        )
        declared = {recipe.output_name for recipe in spec.task.outputs}
        if not isinstance(outputs, dict) or set(outputs) != declared:
            returned = sorted(outputs) if isinstance(outputs, dict) else type(outputs).__name__
            raise RuntimeError(
                f"operation returned {returned!r}; declared ports are {sorted(declared)!r}")

        result_outputs: dict[str, dict[str, str]] = {}
        for port in sorted(declared):
            _validate_port(port)
            relative = Path("outputs") / port / "payload.json"
            _write_once_json(stage / relative, outputs[port])
            result_outputs[port] = {"path": relative.as_posix()}

        _write_once_json(stage / "result.json", {
            "schema": "stage1-attempt-result-v1",
            "attempt_id": spec.attempt_id,
            "attempt_token": spec.attempt_token,
            "outputs": result_outputs,
            # Real peak resident memory for this worker process. Reported so
            # the Stage-8 revision machinery works from a measurement rather
            # than from the envelope somebody reserved.
            "peak_memory_kb": _peak_memory_kb(),
        })
        return 0
    except BaseException as exc:
        # The root result marker is written only on complete success.  Partial
        # payloads remain attempt-scoped and invisible to the controller's
        # artifact committer.
        try:
            _write_once_json(stage / "error.json", {
                "schema": "stage1-attempt-error-v1",
                "attempt_id": spec.attempt_id if spec else "unknown",
                "attempt_token": spec.attempt_token if spec else args.attempt_token,
                "error": f"{type(exc).__name__}: {exc}",
                "failed_at": time.time(),
            })
        except BaseException:
            pass
        print(f"stage1 worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _verify_invocation(spec: AttemptSpec, token: str, stage: Path) -> None:
    if spec.attempt_token != token:
        raise RuntimeError("worker argv token does not match immutable attempt spec")
    if Path(spec.stage_dir).resolve() != stage:
        raise RuntimeError("worker stage directory does not match attempt spec")
    if spec.provider != "stage1-local-subprocess":
        raise RuntimeError("worker refuses an attempt for another provider")
    if operation_component(spec.task.component.operation_key) != spec.task.component:
        raise RuntimeError("worker component binding is not in the closed registry")
    input_names = {binding.input_name for binding in spec.task.inputs}
    if set(spec.input_artifacts) != input_names:
        raise RuntimeError("attempt input artifact bindings do not match task ports")
    for recipe in spec.task.outputs:
        _validate_port(recipe.output_name)


def _peak_memory_kb() -> int:
    """Peak RSS of this worker process, in kilobytes.

    ``ru_maxrss`` is kilobytes on Linux and bytes on macOS; this runtime is
    Linux-only (the site preflight refuses anything else), so the raw value is
    already the unit we want.  Returns 0 when the platform cannot report it,
    which the consumer treats as "no measurement" rather than as zero usage.
    """
    try:
        import resource
        return max(0, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    except (ImportError, OSError, ValueError):  # pragma: no cover - platform
        return 0


def _claim_worker(supervisor: Path, spec: AttemptSpec) -> None:
    pid = os.getpid()
    receipt = {
        "schema": "stage1-local-worker-receipt-v1",
        "attempt_id": spec.attempt_id,
        "attempt_token": spec.attempt_token,
        "pid": pid,
        "pgid": os.getpgrp(),
        "start_ticks": _process_start_ticks(pid),
        "started_at": time.time(),
    }
    # Exclusive creation is a durable token claim.  A duplicate local launch
    # cannot execute the same attempt a second time.
    _write_once_json(supervisor / "worker_receipt.json", receipt,
                     equal_existing_is_error=True)


def _load_inputs(spec: AttemptSpec, runtime_root: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, manifest_raw in sorted(spec.input_artifacts.items()):
        manifest_path = Path(manifest_raw)
        if not manifest_path.is_absolute():
            raise ValueError(f"input manifest for {name!r} must be absolute")
        manifest_path = manifest_path.resolve()
        _require_within(manifest_path, runtime_root, "input manifest")
        manifest = _read_strict_json(manifest_path)
        required = {
            "schema", "artifact_id", "recipe_id", "media_type",
            "content_sha256", "size_bytes", "object_path",
        }
        if set(manifest) != required:
            raise ValueError(f"input manifest for {name!r} has invalid fields")
        if (manifest["schema"] != "stage1-artifact-manifest-v1"
                or manifest["media_type"] != "application/json"):
            raise ValueError(f"input manifest for {name!r} has unsupported schema/media type")

        object_raw = Path(manifest["object_path"])
        object_path = (object_raw if object_raw.is_absolute()
                       else runtime_root / object_raw).resolve()
        _require_within(object_path, runtime_root, "input object")
        payload = object_path.read_bytes()
        if len(payload) != int(manifest["size_bytes"]):
            raise ValueError(f"input object size mismatch for {name!r}")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest["content_sha256"]:
            raise ValueError(f"input object checksum mismatch for {name!r}")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"input object for {name!r} is not strict JSON") from exc
        values[name] = strict_copy(decoded)
    return values


def _validate_port(port: str) -> None:
    if not isinstance(port, str) or not _SAFE_PORT.fullmatch(port):
        raise ValueError(f"unsafe output port name {port!r}")
    if port in {".", ".."}:
        raise ValueError(f"unsafe output port name {port!r}")


def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the runtime root") from exc


def _read_strict_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    value = strict_copy(raw)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object at {path}")
    return value


def _write_once_json(path: Path, value: Any, *,
                     equal_existing_is_error: bool = False) -> None:
    encoded = (strict_canonical_json(value) + "\n").encode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"temporary worker-output collision: {temporary}")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = path.read_bytes()
            if existing != encoded or equal_existing_is_error:
                raise RuntimeError(
                    f"immutable worker output already exists: {path}")
        temporary.unlink(missing_ok=True)
        _fsync_dir(path.parent)
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


if __name__ == "__main__":
    raise SystemExit(main())
