"""Compile one verified scientific invocation into partitioned task identity.

The partition layer is not allowed to restate an operation selected by the
resolver. A template therefore carries the complete, self-verifying
``BoundInvocation``: implementation digest, parameters, input requirements,
and output descriptors all travel together. Partitioning adds only the
partition key and execution-policy metadata.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from capabilities import BoundInvocation
from capabilities.implementation import _digest
from engine.runtime.identity import require_object_fields, strict_hash

from .space import PartitionKey


@dataclass(frozen=True)
class PartitionTaskTemplate:
    """A verified invocation shared by every partition in one collection."""

    template_id: str
    invocation: BoundInvocation
    estimated_cost_units: int
    retry_safe: bool = False

    def __post_init__(self) -> None:
        _digest(self.template_id, "template_id")
        if not isinstance(self.invocation, BoundInvocation):
            raise TypeError("partition template requires a BoundInvocation")
        self.invocation.implementation.verify_current()
        if (isinstance(self.estimated_cost_units, bool)
                or not isinstance(self.estimated_cost_units, int)
                or self.estimated_cost_units < 0):
            raise ValueError("estimated cost must be a non-negative integer")
        if type(self.retry_safe) is not bool:
            raise TypeError("retry_safe must be bool")
        if self.retry_safe and not self.invocation.implementation.retry_safe:
            raise ValueError(
                "automatic retry cannot exceed the implementation contract")
        if self.template_id != self.expected_id():
            raise ValueError("partition task template identity does not verify")

    @classmethod
    def bind(cls, invocation: BoundInvocation, *,
             estimated_cost_units: int | None = None,
             retry_safe: bool = False) -> "PartitionTaskTemplate":
        """Compile from a resolver output; automatic retry is opt-in."""
        if not isinstance(invocation, BoundInvocation):
            raise TypeError("bind requires a BoundInvocation")
        if estimated_cost_units is None:
            value = invocation.metric_estimates.get("cost_units", 1)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "bound invocation cost_units must be an integer")
            estimated_cost_units = value
        payload = cls._payload(invocation, estimated_cost_units, retry_safe)
        return cls(strict_hash(payload), invocation, estimated_cost_units,
                   retry_safe)

    @staticmethod
    def _payload(invocation: BoundInvocation, estimated_cost_units: int,
                 retry_safe: bool) -> dict[str, Any]:
        return {
            "schema": "stage7-partition-task-template-v2",
            "invocation": invocation.to_dict(),
            "estimated_cost_units": estimated_cost_units,
            "retry_safe": retry_safe,
        }

    @property
    def invocation_key(self) -> str:
        return self.invocation.invocation_key

    @property
    def capability_id(self) -> str:
        return self.invocation.capability_id

    @property
    def operation_key(self) -> str:
        return self.invocation.implementation.operation_key

    @property
    def implementation_sha256(self) -> str:
        return self.invocation.implementation.implementation_sha256

    @property
    def input_slot_ids(self) -> tuple[str, ...]:
        return tuple(use.requirement_use_id for use in self.invocation.input_uses)

    @property
    def parameters(self) -> dict[str, Any]:
        return self.invocation.parameters

    @property
    def outputs(self):
        return self.invocation.outputs

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.invocation, self.estimated_cost_units, self.retry_safe))

    def logical_task_key(self, partition: PartitionKey) -> str:
        """Stable identity for this exact invocation and one partition."""
        if not isinstance(partition, PartitionKey):
            raise TypeError("logical_task_key requires a PartitionKey")
        return strict_hash({
            "schema": "stage7-logical-task-key-v2",
            "invocation_key": self.invocation_key,
            "template_id": self.template_id,
            "partition_key_id": partition.key_id,
        })

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.invocation, self.estimated_cost_units, self.retry_safe)
        payload["template_id"] = self.template_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionTaskTemplate":
        raw = require_object_fields(
            value,
            {"schema", "template_id", "invocation",
             "estimated_cost_units", "retry_safe"},
            "PartitionTaskTemplate")
        if raw.pop("schema") != "stage7-partition-task-template-v2":
            raise ValueError(
                "PartitionTaskTemplate schema is not "
                "stage7-partition-task-template-v2")
        raw["invocation"] = BoundInvocation.from_dict(raw["invocation"])
        return cls(**raw)


__all__ = ["PartitionTaskTemplate"]
