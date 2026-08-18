"""Exact committed-artifact inputs for executable partitions.

Partition coordinates are not data.  A tile label such as ``tile=t003`` may
help a planner choose data, but it cannot be handed to a worker as though it
were the data itself.  This module records the narrow bridge that is safe to
execute: every input port of every packet member is bound to one exact,
content-addressed Stage-1 artifact and to the descriptor identity the planner
expects that authoritative artifact recipe to carry.

The manifest is deliberately packet-sized.  It therefore preserves the
bounded-memory property of Stage 7 while binding the collection, partition
space, scientific task template, packet membership, and every artifact choice
into one tamper-evident identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import require_object_fields, strict_hash

from .packet import WorkPacket
from .space import PartitionSetSpec
from .template import PartitionTaskTemplate


@dataclass(frozen=True)
class PartitionArtifactInput:
    """One invocation-local port bound to one committed artifact identity."""

    requirement_use_id: str
    input_name: str
    artifact_id: str
    descriptor_id: str

    def __post_init__(self) -> None:
        _digest(self.requirement_use_id, "partition input requirement_use_id")
        _required_text(self.input_name, "partition input name")
        _digest(self.artifact_id, "partition input artifact_id")
        _digest(self.descriptor_id, "partition input descriptor_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "requirement_use_id": self.requirement_use_id,
            "input_name": self.input_name,
            "artifact_id": self.artifact_id,
            "descriptor_id": self.descriptor_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionArtifactInput":
        return cls(**require_object_fields(
            value,
            {"requirement_use_id", "input_name", "artifact_id",
             "descriptor_id"},
            "PartitionArtifactInput",
        ))


@dataclass(frozen=True)
class PartitionInputAssignment:
    """The complete external input vector for one logical partition task."""

    logical_task_key: str
    partition_index: int
    inputs: tuple[PartitionArtifactInput, ...]

    def __post_init__(self) -> None:
        _digest(self.logical_task_key, "partition input logical_task_key")
        if (isinstance(self.partition_index, bool)
                or not isinstance(self.partition_index, int)
                or self.partition_index < 0):
            raise ValueError("partition input index must be non-negative")
        if (not isinstance(self.inputs, tuple)
                or not self.inputs
                or not all(isinstance(value, PartitionArtifactInput)
                           for value in self.inputs)):
            raise TypeError(
                "partition input assignment needs a non-empty typed input tuple")
        names = tuple(value.input_name for value in self.inputs)
        uses = tuple(value.requirement_use_id for value in self.inputs)
        if names != tuple(sorted(names)):
            raise ValueError("partition inputs must be sorted by input name")
        if len(set(names)) != len(names) or len(set(uses)) != len(uses):
            raise ValueError("partition input ports and uses must be unique")

    @classmethod
    def bind(
            cls, logical_task_key: str, partition_index: int,
            inputs: Iterable[PartitionArtifactInput],
    ) -> "PartitionInputAssignment":
        return cls(
            logical_task_key,
            partition_index,
            tuple(sorted(tuple(inputs), key=lambda value: value.input_name)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_task_key": self.logical_task_key,
            "partition_index": self.partition_index,
            "inputs": [value.to_dict() for value in self.inputs],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionInputAssignment":
        raw = require_object_fields(
            value, {"logical_task_key", "partition_index", "inputs"},
            "PartitionInputAssignment")
        if not isinstance(raw["inputs"], list):
            raise ValueError("PartitionInputAssignment.inputs must be an array")
        raw["inputs"] = tuple(
            PartitionArtifactInput.from_dict(item) for item in raw["inputs"])
        return cls(**raw)


@dataclass(frozen=True)
class PartitionInputManifest:
    """Tamper-evident exact inputs for every member of one work packet."""

    manifest_id: str
    collection_id: str
    set_id: str
    template_id: str
    packet_id: str
    assignments: tuple[PartitionInputAssignment, ...]

    def __post_init__(self) -> None:
        for value, label in (
                (self.manifest_id, "partition input manifest_id"),
                (self.collection_id, "partition input collection_id"),
                (self.set_id, "partition input set_id"),
                (self.template_id, "partition input template_id"),
                (self.packet_id, "partition input packet_id")):
            _digest(value, label)
        if (not isinstance(self.assignments, tuple)
                or not self.assignments
                or not all(isinstance(value, PartitionInputAssignment)
                           for value in self.assignments)):
            raise TypeError(
                "partition input manifest needs a non-empty assignment tuple")
        indices = tuple(value.partition_index for value in self.assignments)
        keys = tuple(value.logical_task_key for value in self.assignments)
        if indices != tuple(sorted(indices)):
            raise ValueError(
                "partition input assignments must be in partition order")
        if len(set(indices)) != len(indices) or len(set(keys)) != len(keys):
            raise ValueError(
                "partition input manifest cannot repeat a partition or task")
        if self.manifest_id != self.expected_id():
            raise ValueError("partition input manifest identity does not verify")

    @classmethod
    def bind(
            cls, collection_id: str, spec: PartitionSetSpec,
            template: PartitionTaskTemplate, packet: WorkPacket,
            assignments: Iterable[PartitionInputAssignment],
    ) -> "PartitionInputManifest":
        values = tuple(sorted(
            tuple(assignments), key=lambda value: value.partition_index))
        payload = cls._payload(
            collection_id, spec.set_id, template.template_id,
            packet.packet_id, values)
        result = cls(
            strict_hash(payload), collection_id, spec.set_id,
            template.template_id, packet.packet_id, values)
        result.verify_for(spec, template, packet)
        return result

    @staticmethod
    def _payload(
            collection_id: str, set_id: str, template_id: str,
            packet_id: str,
            assignments: tuple[PartitionInputAssignment, ...],
    ) -> dict[str, Any]:
        return {
            "schema": "stage8r-partition-input-manifest-v1",
            "collection_id": collection_id,
            "set_id": set_id,
            "template_id": template_id,
            "packet_id": packet_id,
            "assignments": [value.to_dict() for value in assignments],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.collection_id, self.set_id, self.template_id,
            self.packet_id, self.assignments))

    def verify_for(
            self, spec: PartitionSetSpec, template: PartitionTaskTemplate,
            packet: WorkPacket,
    ) -> None:
        """Replay collection membership and the invocation's exact ports."""
        if not isinstance(spec, PartitionSetSpec):
            raise TypeError("partition input verification requires a set spec")
        if not isinstance(template, PartitionTaskTemplate):
            raise TypeError("partition input verification requires a template")
        if not isinstance(packet, WorkPacket):
            raise TypeError("partition input verification requires a work packet")
        if (self.set_id != spec.set_id
                or self.template_id != template.template_id
                or self.collection_id != packet.collection_id
                or self.packet_id != packet.packet_id
                or packet.template_id != template.template_id):
            raise ValueError(
                "partition input manifest belongs to another collection, "
                "space, template, or packet")
        expected_members = tuple(
            (member.partition_index, member.logical_task_key)
            for member in packet.members)
        actual_members = tuple(
            (value.partition_index, value.logical_task_key)
            for value in self.assignments)
        if actual_members != expected_members:
            raise ValueError(
                "partition input assignments do not exactly match packet members")
        uses = {
            value.requirement_use_id: value
            for value in template.invocation.input_uses
        }
        expected_ports = {
            (value.requirement_use_id, value.port_id)
            for value in template.invocation.input_uses
        }
        if not expected_ports:
            raise ValueError(
                "an input-free partition template cannot take an input manifest")
        for assignment in self.assignments:
            key = template.logical_task_key(spec.key_at(
                assignment.partition_index))
            if key != assignment.logical_task_key:
                raise ValueError(
                    "partition input assignment key does not match its exact "
                    "partition and template")
            actual_ports = {
                (value.requirement_use_id, value.input_name)
                for value in assignment.inputs
            }
            if actual_ports != expected_ports:
                raise ValueError(
                    "partition input assignment does not bind every exact "
                    "invocation requirement use and port")
            for value in assignment.inputs:
                use = uses[value.requirement_use_id]
                if not (use.cardinality.minimum <= 1
                        <= use.cardinality.maximum):
                    raise ValueError(
                        "partition execution supports exactly one artifact per "
                        f"runtime input, but {use.port_id!r} excludes one")

    def assignment_for(self, logical_task_key: str
                       ) -> PartitionInputAssignment:
        try:
            return next(value for value in self.assignments
                        if value.logical_task_key == logical_task_key)
        except StopIteration as exc:
            raise KeyError(logical_task_key) from exc

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.collection_id, self.set_id, self.template_id,
            self.packet_id, self.assignments)
        payload["manifest_id"] = self.manifest_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionInputManifest":
        raw = require_object_fields(
            value,
            {"schema", "manifest_id", "collection_id", "set_id",
             "template_id", "packet_id", "assignments"},
            "PartitionInputManifest",
        )
        if raw.pop("schema") != "stage8r-partition-input-manifest-v1":
            raise ValueError(
                "PartitionInputManifest schema is not "
                "stage8r-partition-input-manifest-v1")
        if not isinstance(raw["assignments"], list):
            raise ValueError("PartitionInputManifest.assignments must be an array")
        raw["assignments"] = tuple(
            PartitionInputAssignment.from_dict(item)
            for item in raw["assignments"])
        return cls(**raw)


__all__ = [
    "PartitionArtifactInput",
    "PartitionInputAssignment",
    "PartitionInputManifest",
]
