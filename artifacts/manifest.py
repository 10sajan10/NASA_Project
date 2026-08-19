"""Portable, content-addressed manifests for target-derived workflows."""
from __future__ import annotations

from dataclasses import dataclass
import re
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from contracts import RequirementUse
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_canonical_json,
    strict_copy,
    strict_hash,
    strict_json_loads,
)

from .records import ArtifactRecord
from .service import ArtifactWorkflowOutcome


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class WorkflowManifest:
    """A standalone scientific planning record, not an execution graph.

    It carries the complete target requirements, selected typed invocations,
    exact satisfaction choices, and native artifact pointers.  A receiver can
    inspect and identity-check it without access to the live SQLite registry.
    """

    manifest_id: str
    target_id: str
    root_uses: tuple[RequirementUse, ...]
    resolution_id: str
    resolution_status: str
    eligible_for_binding: bool
    artifact_snapshot_id: str
    plan_id: str | None
    total_cost_units: int | None
    selected_invocations: tuple[dict[str, Any], ...]
    satisfaction_bindings: tuple[dict[str, Any], ...]
    selected_artifacts: tuple[ArtifactRecord, ...]
    discovery_certificate: dict[str, Any]
    discovery_universe: dict[str, Any]

    def __post_init__(self) -> None:
        for value, label in (
                (self.manifest_id, "workflow manifest ID"),
                (self.target_id, "workflow target ID"),
                (self.resolution_id, "workflow resolution ID"),
                (self.artifact_snapshot_id, "workflow artifact snapshot ID")):
            _digest(value, label)
        if self.plan_id is not None:
            _digest(self.plan_id, "workflow plan ID")
        if (not isinstance(self.root_uses, tuple)
                or not all(isinstance(value, RequirementUse)
                           for value in self.root_uses)
                or self.root_uses != tuple(sorted(
                    self.root_uses, key=lambda value: value.requirement_use_id))):
            raise ValueError("workflow manifest roots must be typed and sorted")
        if type(self.eligible_for_binding) is not bool:
            raise TypeError("workflow manifest eligibility must be bool")
        if self.total_cost_units is not None and (
                isinstance(self.total_cost_units, bool)
                or not isinstance(self.total_cost_units, int)
                or self.total_cost_units < 0):
            raise ValueError("workflow manifest cost must be non-negative")
        if self.selected_artifacts != tuple(sorted(
                self.selected_artifacts, key=lambda value: value.record_id)):
            raise ValueError("workflow manifest artifacts must be sorted")
        for name in ("selected_invocations", "satisfaction_bindings"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                    isinstance(value, dict) for value in values):
                raise TypeError(f"workflow manifest {name} must be JSON objects")
            object.__setattr__(self, name, tuple(
                freeze_json(value) for value in values))
        object.__setattr__(self, "discovery_certificate",
                           freeze_json(self.discovery_certificate))
        object.__setattr__(self, "discovery_universe",
                           freeze_json(self.discovery_universe))
        if self.manifest_id != self.expected_id():
            raise ValueError("workflow manifest identity does not verify")

    @classmethod
    def bind(
        cls,
        target_id: str,
        roots: Iterable[RequirementUse],
        outcome: ArtifactWorkflowOutcome,
    ) -> "WorkflowManifest":
        root_values = tuple(sorted(
            roots, key=lambda value: value.requirement_use_id))
        plan = outcome.resolution.selection.plan
        selected_ids = set(plan.selected_invocation_ids) if plan else set()
        invocations = tuple(
            node.to_dict() for node in outcome.resolution.hypergraph.invocation_nodes
            if node.invocation_id in selected_ids
        )
        satisfactions = tuple(
            value.to_dict() for value in (plan.satisfactions if plan else ()))
        values = {
            "target_id": target_id,
            "root_uses": root_values,
            "resolution_id": outcome.resolution.resolution_id,
            "resolution_status": outcome.resolution.status.value,
            "eligible_for_binding": outcome.resolution.eligible_for_binding,
            "artifact_snapshot_id": outcome.artifact_snapshot.snapshot_id,
            "plan_id": plan.plan_id if plan else None,
            "total_cost_units": plan.total_cost_units if plan else None,
            "selected_invocations": invocations,
            "satisfaction_bindings": satisfactions,
            "selected_artifacts": outcome.selected_artifacts,
            "discovery_certificate":
                outcome.resolution.discovery_certificate.to_dict(),
            "discovery_universe":
                outcome.resolution.discovery_universe.to_dict(),
        }
        provisional = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(provisional, name, value)
        object.__setattr__(provisional, "manifest_id", "")
        return cls(manifest_id=strict_hash(provisional._payload()), **values)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "stage10b-portable-workflow-manifest-v1",
            "target_id": self.target_id,
            "root_uses": [value.to_dict() for value in self.root_uses],
            "resolution_id": self.resolution_id,
            "resolution_status": self.resolution_status,
            "eligible_for_binding": self.eligible_for_binding,
            "artifact_snapshot_id": self.artifact_snapshot_id,
            "plan_id": self.plan_id,
            "total_cost_units": self.total_cost_units,
            "selected_invocations": [strict_copy(value)
                                     for value in self.selected_invocations],
            "satisfaction_bindings": [strict_copy(value)
                                      for value in self.satisfaction_bindings],
            "selected_artifacts": [value.to_dict()
                                   for value in self.selected_artifacts],
            "discovery_certificate": strict_copy(self.discovery_certificate),
            "discovery_universe": strict_copy(self.discovery_universe),
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {"manifest_id": self.manifest_id, **self._payload()}

    def write_json(self, path: str | Path) -> Path:
        """Atomically export canonical JSON for transfer to another service."""
        target = Path(path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = strict_canonical_json(self.to_dict()) + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    @classmethod
    def read_json(cls, path: str | Path) -> "WorkflowManifest":
        value = strict_json_loads(Path(path).read_bytes())
        if not isinstance(value, dict):
            raise TypeError("workflow manifest file must contain an object")
        return cls.from_dict(value)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowManifest":
        raw = require_object_fields(value, {
            "schema", "manifest_id", "target_id", "root_uses",
            "resolution_id", "resolution_status", "eligible_for_binding",
            "artifact_snapshot_id", "plan_id", "total_cost_units",
            "selected_invocations", "satisfaction_bindings",
            "selected_artifacts", "discovery_certificate",
            "discovery_universe",
        }, "WorkflowManifest")
        if raw.pop("schema") != "stage10b-portable-workflow-manifest-v1":
            raise ValueError("unsupported workflow manifest schema")
        for name in ("root_uses", "selected_invocations",
                     "satisfaction_bindings", "selected_artifacts"):
            if not isinstance(raw[name], list):
                raise TypeError(f"WorkflowManifest.{name} must be an array")
        raw["root_uses"] = tuple(
            RequirementUse.from_dict(item) for item in raw["root_uses"])
        raw["selected_invocations"] = tuple(raw["selected_invocations"])
        raw["satisfaction_bindings"] = tuple(raw["satisfaction_bindings"])
        raw["selected_artifacts"] = tuple(
            ArtifactRecord.from_dict(item) for item in raw["selected_artifacts"])
        return cls(**raw)


__all__ = ["WorkflowManifest"]
