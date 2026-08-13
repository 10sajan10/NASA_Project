"""Stage-0 execution identity helpers.

The legacy engine used producer names as both plan identity and runtime lookup
keys.  That permits a mutable ``ProducerRegistry`` to change what an already
constructed pipeline executes.  This module gives the baseline runner a small,
deterministic component identity without introducing the Stage-1 task/attempt
model.

Identity rules
--------------

* ``component_version`` (or ``version``) is authoritative when a component
  declares it.
* Otherwise the version is the SHA-256 of the implementation source files.
* Instance configuration contributes only a SHA-256 digest.  Configuration
  values are not serialized into the plan, so credential-like fields cannot
  leak through lineage.
* Determinism and idempotency are explicit declarations; absence is recorded as
  ``unspecified`` rather than guessed.

These rules freeze the current runner.  They are intentionally smaller than the
scientific and deployment identities introduced by later stages.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .registry import producer_produces, producer_requires


_RUNTIME_FIELDS = {
    "_calls", "calls", "call_count", "thread_ids", "lock", "live", "peak",
    "finalized",
}


def canonical_json(value: Any) -> str:
    """Return stable JSON for hashes and baseline manifests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _source_files(component: Any) -> list[Path]:
    targets = [component, type(component)]
    for attr in ("driver", "func"):
        nested = getattr(component, attr, None)
        if nested is not None:
            targets.extend((nested, type(nested)))
    paths: set[Path] = set()
    for target in targets:
        try:
            raw = inspect.getsourcefile(target) or inspect.getfile(target)
            path = Path(raw).resolve()
            if path.is_file():
                paths.add(path)
        except (OSError, TypeError):
            continue
    return sorted(paths, key=str)


def _logical_source_path(path: Path) -> str:
    """Return a location-independent source label for plan identity."""
    project_root = Path(__file__).resolve().parents[1]
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        # External package location is deployment provenance, not scientific
        # identity. Content plus the basename remains stable across venv roots.
        return f"external/{path.name}"


def _implementation_sha256(component: Any) -> tuple[str, tuple[str, ...]]:
    digest = hashlib.sha256()
    files = _source_files(component)
    for path in files:
        digest.update(_logical_source_path(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    if not files:
        # Builtins/dynamic test doubles still receive a stable class identity.
        digest.update(
            f"{type(component).__module__}:{type(component).__qualname__}"
            .encode("utf-8"))
    return digest.hexdigest(), tuple(_logical_source_path(p) for p in files)


def _canonical_value(value: Any, depth: int = 0) -> Any:
    """Reduce component configuration to bounded JSON-compatible values."""
    if depth > 4:
        return f"<{type(value).__module__}.{type(value).__qualname__}>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (Path, date, datetime, Enum)):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(),
                "bytes": len(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v, depth + 1) for v in value]
    if isinstance(value, set):
        vals = [_canonical_value(v, depth + 1) for v in value]
        return sorted(vals, key=canonical_json)
    if isinstance(value, dict):
        return {
            str(k): _canonical_value(v, depth + 1)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _canonical_value(dataclasses.asdict(value), depth + 1)
    if callable(value):
        return {
            "callable": f"{getattr(value, '__module__', '')}:"
                        f"{getattr(value, '__qualname__', type(value).__qualname__)}"
        }
    state = getattr(value, "__dict__", None)
    if isinstance(state, dict):
        selected = {
            k: _canonical_value(v, depth + 1)
            for k, v in sorted(state.items())
            if not k.startswith("_") and k not in _RUNTIME_FIELDS
        }
        return {
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
            "state": selected,
        }
    return f"<{type(value).__module__}.{type(value).__qualname__}>"


def _component_config(component: Any) -> dict[str, Any]:
    explicit = getattr(component, "component_config", None)
    if callable(explicit):
        return _canonical_value(explicit())
    state = getattr(component, "__dict__", {})
    return {
        k: _canonical_value(v)
        for k, v in sorted(state.items())
        if not k.startswith("_") and k not in _RUNTIME_FIELDS
    }


@dataclass(frozen=True)
class ResourceEnvelope:
    memory_budget_mb: int | None
    cost_hint: str | None
    tile_parallel: bool | None
    requires_barrier: bool | None


@dataclass(frozen=True)
class ComponentBinding:
    """Immutable identity record for one producer object."""

    name: str
    module: str
    qualname: str
    version: str
    implementation_sha256: str
    configuration_sha256: str
    source_files: tuple[str, ...]
    produces: tuple[str, ...]
    requires: tuple[str, ...]
    deterministic: str
    idempotent: str
    resource_envelope: ResourceEnvelope

    @classmethod
    def from_component(cls, component: Any) -> "ComponentBinding":
        impl_sha, source_files = _implementation_sha256(component)
        declared_version = (getattr(component, "component_version", None)
                            or getattr(component, "version", None))
        version = str(declared_version or f"source:{impl_sha[:16]}")
        capabilities = getattr(component, "capabilities", None)
        cost_hint = getattr(capabilities, "cost_hint", None)
        resource = ResourceEnvelope(
            memory_budget_mb=getattr(
                capabilities, "memory_budget_mb", None),
            cost_hint=getattr(cost_hint, "value", cost_hint),
            tile_parallel=getattr(capabilities, "tile_parallel", None),
            requires_barrier=getattr(
                capabilities, "requires_barrier", None),
        )
        return cls(
            name=str(component.name),
            module=type(component).__module__,
            qualname=type(component).__qualname__,
            version=version,
            implementation_sha256=impl_sha,
            configuration_sha256=sha256_json(_component_config(component)),
            source_files=source_files,
            produces=producer_produces(component),
            requires=producer_requires(component),
            deterministic=str(getattr(component, "deterministic", "unspecified")),
            idempotent=str(getattr(component, "idempotent", "unspecified")),
            resource_envelope=resource,
        )

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def git_revision(root: Path | None = None) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root or Path.cwd()), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return None


def git_worktree_dirty(root: Path | None = None) -> bool | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(root or Path.cwd()), "status", "--porcelain"],
            stderr=subprocess.DEVNULL, text=True)
        return bool(output.strip())
    except Exception:
        return None


def execution_engine_identity() -> dict[str, Any]:
    """Hash the Stage-0 binding/runner implementation itself."""
    digest = hashlib.sha256()
    files: list[str] = []
    for name in ("identity.py", "pipeline.py", "scheduler.py"):
        path = Path(__file__).with_name(name).resolve()
        files.append(str(path))
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {
        "sha256": digest.hexdigest(),
        "source_files": files,
        "python": sys.version.splitlines()[0],
    }
