"""Authoritative metadata records for native scientific artifacts.

An artifact is not a Cube row and it is not a payload copy.  It is the exact
scientific descriptor, immutable content identity, provenance, and the
verified location of bytes that remain in their native representation.
"""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from capabilities import ArtifactLeaf
from contracts import ArtifactDescriptor
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")


@dataclass(frozen=True)
class ArtifactInput:
    """Exact upstream artifact bound to one producer input port."""

    port_id: str
    artifact_id: str

    def __post_init__(self) -> None:
        _text(self.port_id, "artifact input port_id")
        _digest(self.artifact_id, "artifact input artifact_id")

    def to_dict(self) -> dict[str, str]:
        return {"port_id": self.port_id, "artifact_id": self.artifact_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactInput":
        return cls(**require_object_fields(
            value, {"port_id", "artifact_id"}, "ArtifactInput"))


@dataclass(frozen=True)
class ArtifactRecord:
    """One immutable, scientifically typed pointer to native bytes.

    ``artifact_id`` names scientific content plus derivation.  The manifest
    root additionally binds the exact local location and byte extent.  Moving
    the same artifact therefore preserves its artifact identity but produces a
    new manifest-backed leaf, which is the correct planning behavior.
    """

    record_id: str
    artifact_id: str
    manifest_root_sha256: str
    descriptor: ArtifactDescriptor
    location: str
    media_type: str
    content_sha256: str
    size_bytes: int
    producer_id: str
    producer_version: str
    output_port_id: str
    inputs: tuple[ArtifactInput, ...] = ()
    evidence_profile_id: str = "evidence:unknown"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, label in (
            (self.record_id, "artifact record_id"),
            (self.artifact_id, "artifact_id"),
            (self.manifest_root_sha256, "artifact manifest root"),
            (self.content_sha256, "artifact content digest"),
        ):
            _digest(value, label)
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("artifact record requires an ArtifactDescriptor")
        for value, label in (
            (self.location, "artifact location"),
            (self.media_type, "artifact media_type"),
            (self.producer_id, "artifact producer_id"),
            (self.producer_version, "artifact producer_version"),
            (self.output_port_id, "artifact output_port_id"),
            (self.evidence_profile_id, "artifact evidence_profile_id"),
        ):
            _text(value, label)
        if not Path(self.location).is_absolute():
            raise ValueError("artifact location must be an absolute local path")
        if self.media_type != self.descriptor.representation:
            raise ValueError(
                "artifact media_type must equal descriptor representation")
        if (isinstance(self.size_bytes, bool)
                or not isinstance(self.size_bytes, int)
                or self.size_bytes < 0):
            raise ValueError("artifact size_bytes must be a non-negative integer")
        if (not isinstance(self.inputs, tuple)
                or not all(isinstance(value, ArtifactInput)
                           for value in self.inputs)):
            raise TypeError("artifact inputs must be typed immutable values")
        ordered = tuple(sorted(
            self.inputs, key=lambda value: (value.port_id, value.artifact_id)))
        if self.inputs != ordered:
            raise ValueError("artifact inputs must be canonically sorted")
        keys = [(value.port_id, value.artifact_id) for value in self.inputs]
        if len(keys) != len(set(keys)):
            raise ValueError("artifact inputs cannot repeat")
        frozen_metadata = freeze_json(self.metadata)
        if not isinstance(frozen_metadata, dict):
            raise TypeError("artifact metadata must be a JSON object")
        object.__setattr__(self, "metadata", frozen_metadata)
        if self.artifact_id != self.expected_artifact_id():
            raise ValueError("artifact identity does not verify")
        if self.manifest_root_sha256 != self.expected_manifest_root():
            raise ValueError("artifact manifest identity does not verify")
        if self.record_id != self.expected_id():
            raise ValueError("artifact record identity does not verify")

    @classmethod
    def bind(
        cls,
        *,
        descriptor: ArtifactDescriptor,
        location: str,
        media_type: str,
        content_sha256: str,
        size_bytes: int,
        producer_id: str,
        producer_version: str,
        output_port_id: str,
        inputs: Iterable[ArtifactInput] = (),
        evidence_profile_id: str = "evidence:unknown",
        metadata: dict[str, Any] | None = None,
    ) -> "ArtifactRecord":
        input_values = tuple(sorted(
            inputs, key=lambda value: (value.port_id, value.artifact_id)))
        base = {
            "descriptor": descriptor,
            "location": location,
            "media_type": media_type,
            "content_sha256": content_sha256,
            "size_bytes": size_bytes,
            "producer_id": producer_id,
            "producer_version": producer_version,
            "output_port_id": output_port_id,
            "inputs": input_values,
            "evidence_profile_id": evidence_profile_id,
            "metadata": strict_copy(metadata or {}),
        }
        artifact_id = strict_hash(cls._artifact_payload(base))
        manifest_root = strict_hash(cls._manifest_payload(base, artifact_id))
        record_id = strict_hash(cls._record_payload(
            base, artifact_id, manifest_root))
        return cls(
            record_id=record_id,
            artifact_id=artifact_id,
            manifest_root_sha256=manifest_root,
            **base,
        )

    @staticmethod
    def _artifact_payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "stage10a-artifact-v1",
            "descriptor_id": values["descriptor"].descriptor_id,
            "content_sha256": values["content_sha256"],
            "producer_id": values["producer_id"],
            "producer_version": values["producer_version"],
            "output_port_id": values["output_port_id"],
            "inputs": [value.to_dict() for value in values["inputs"]],
        }

    @staticmethod
    def _manifest_payload(
        values: dict[str, Any], artifact_id: str,
    ) -> dict[str, Any]:
        return {
            "schema": "stage10a-local-artifact-manifest-v1",
            "artifact_id": artifact_id,
            "location": values["location"],
            "media_type": values["media_type"],
            "content_sha256": values["content_sha256"],
            "size_bytes": values["size_bytes"],
        }

    @staticmethod
    def _record_payload(
        values: dict[str, Any], artifact_id: str, manifest_root: str,
    ) -> dict[str, Any]:
        return {
            "schema": "stage10a-artifact-record-v1",
            "artifact_id": artifact_id,
            "manifest_root_sha256": manifest_root,
            "descriptor": values["descriptor"].to_dict(),
            "evidence_profile_id": values["evidence_profile_id"],
            "metadata": strict_copy(values["metadata"]),
        }

    def expected_artifact_id(self) -> str:
        return strict_hash(self._artifact_payload({
            "descriptor": self.descriptor,
            "content_sha256": self.content_sha256,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "output_port_id": self.output_port_id,
            "inputs": self.inputs,
        }))

    def expected_manifest_root(self) -> str:
        return strict_hash(self._manifest_payload({
            "location": self.location,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
        }, self.artifact_id))

    def expected_id(self) -> str:
        return strict_hash(self._record_payload({
            "descriptor": self.descriptor,
            "evidence_profile_id": self.evidence_profile_id,
            "metadata": self.metadata,
        }, self.artifact_id, self.manifest_root_sha256))

    @property
    def leaf(self) -> ArtifactLeaf:
        return ArtifactLeaf.bind(
            artifact_id=self.artifact_id,
            manifest_root_sha256=self.manifest_root_sha256,
            descriptor=self.descriptor,
            evidence_profile_id=self.evidence_profile_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "artifact_id": self.artifact_id,
            "manifest_root_sha256": self.manifest_root_sha256,
            "descriptor": self.descriptor.to_dict(),
            "location": self.location,
            "media_type": self.media_type,
            "content_sha256": self.content_sha256,
            "size_bytes": self.size_bytes,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "output_port_id": self.output_port_id,
            "inputs": [value.to_dict() for value in self.inputs],
            "evidence_profile_id": self.evidence_profile_id,
            "metadata": strict_copy(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactRecord":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ArtifactRecord")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        if not isinstance(raw["inputs"], list):
            raise TypeError("ArtifactRecord.inputs must be an array")
        raw["inputs"] = tuple(
            ArtifactInput.from_dict(item) for item in raw["inputs"])
        return cls(**raw)


class ArtifactAvailability(str, Enum):
    COMMITTED = "COMMITTED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ArtifactSnapshotEntry:
    record: ArtifactRecord
    availability: ArtifactAvailability
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.record, ArtifactRecord):
            raise TypeError("artifact snapshot entry requires a record")
        if not isinstance(self.availability, ArtifactAvailability):
            raise TypeError("artifact snapshot availability must be typed")
        if self.availability is ArtifactAvailability.UNAVAILABLE:
            _text(self.reason, "unavailable artifact reason")
        elif self.reason:
            raise ValueError("committed artifact cannot carry a failure reason")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "availability": self.availability.value,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactSnapshotEntry":
        raw = require_object_fields(
            value, {"record", "availability", "reason"},
            "ArtifactSnapshotEntry")
        raw["record"] = ArtifactRecord.from_dict(raw["record"])
        raw["availability"] = ArtifactAvailability(raw["availability"])
        return cls(**raw)


@dataclass(frozen=True)
class ArtifactRegistrySnapshot:
    snapshot_id: str
    entries: tuple[ArtifactSnapshotEntry, ...]

    def __post_init__(self) -> None:
        _digest(self.snapshot_id, "artifact registry snapshot_id")
        if (not isinstance(self.entries, tuple)
                or not all(isinstance(value, ArtifactSnapshotEntry)
                           for value in self.entries)):
            raise TypeError("artifact snapshot entries must be immutable")
        if self.entries != tuple(sorted(
                self.entries, key=lambda value: value.record.record_id)):
            raise ValueError("artifact snapshot entries must be sorted")
        if self.snapshot_id != self.expected_id():
            raise ValueError("artifact registry snapshot identity does not verify")

    @classmethod
    def freeze(
        cls, entries: Iterable[ArtifactSnapshotEntry],
    ) -> "ArtifactRegistrySnapshot":
        values = tuple(sorted(entries, key=lambda value: value.record.record_id))
        payload = {
            "schema": "stage10a-artifact-registry-snapshot-v1",
            "entries": [value.to_dict() for value in values],
        }
        return cls(strict_hash(payload), values)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage10a-artifact-registry-snapshot-v1",
            "entries": [value.to_dict() for value in self.entries],
        })

    @property
    def committed_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(value.record for value in self.entries
                     if value.availability is ArtifactAvailability.COMMITTED)

    def record_for_leaf(self, leaf_id: str) -> ArtifactRecord:
        for value in self.entries:
            if value.record.leaf.leaf_id == leaf_id:
                return value.record
        raise KeyError(leaf_id)

    def planning_inputs(self):
        """Return the Stage-3 leaf declarations and trusted availability."""
        if not self.entries:
            return (), None
        from resolution import (
            ArtifactAvailabilitySnapshot,
            ArtifactCommitRecord,
            ArtifactCommitStatus,
        )
        leaves = tuple(value.record.leaf for value in self.entries)
        availability = ArtifactAvailabilitySnapshot.freeze(
            ArtifactCommitRecord(
                value.record.leaf.leaf_id,
                ArtifactCommitStatus.COMMITTED
                if value.availability is ArtifactAvailability.COMMITTED
                else ArtifactCommitStatus.UNAVAILABLE,
            )
            for value in self.entries
        )
        return leaves, availability

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "entries": [value.to_dict() for value in self.entries],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactRegistrySnapshot":
        raw = require_object_fields(
            value, {"snapshot_id", "entries"}, "ArtifactRegistrySnapshot")
        if not isinstance(raw["entries"], list):
            raise TypeError("ArtifactRegistrySnapshot.entries must be an array")
        raw["entries"] = tuple(
            ArtifactSnapshotEntry.from_dict(item) for item in raw["entries"])
        return cls(**raw)


__all__ = [
    "ArtifactAvailability",
    "ArtifactInput",
    "ArtifactRecord",
    "ArtifactRegistrySnapshot",
    "ArtifactSnapshotEntry",
]
