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
class PartitionRetryPolicy:
    """Immutable automatic-retry authority for a partition collection.

    Both the safety decision and its attempt ceiling are scientific execution
    policy.  Keeping them together in the template identity prevents a packet
    result caller from increasing either value after the collection was
    registered.
    """

    retry_safe: bool = False
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if type(self.retry_safe) is not bool:
            raise TypeError("retry_safe must be bool")
        if (isinstance(self.max_attempts, bool)
                or not isinstance(self.max_attempts, int)
                or self.max_attempts < 1):
            raise ValueError("max_attempts must be a positive integer")
        if not self.retry_safe and self.max_attempts != 1:
            raise ValueError(
                "a non-retry-safe policy must have max_attempts=1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage8r-partition-retry-policy-v1",
            "retry_safe": self.retry_safe,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionRetryPolicy":
        raw = require_object_fields(
            value, {"schema", "retry_safe", "max_attempts"},
            "PartitionRetryPolicy")
        if raw.pop("schema") != "stage8r-partition-retry-policy-v1":
            raise ValueError(
                "PartitionRetryPolicy schema is not "
                "stage8r-partition-retry-policy-v1")
        return cls(**raw)


@dataclass(frozen=True)
class PartitionTaskTemplate:
    """A verified invocation shared by every partition in one collection."""

    template_id: str
    invocation: BoundInvocation
    estimated_cost_units: int
    retry_policy: PartitionRetryPolicy

    def __post_init__(self) -> None:
        _digest(self.template_id, "template_id")
        if not isinstance(self.invocation, BoundInvocation):
            raise TypeError("partition template requires a BoundInvocation")
        self.invocation.implementation.verify_current()
        if (isinstance(self.estimated_cost_units, bool)
                or not isinstance(self.estimated_cost_units, int)
                or self.estimated_cost_units < 0):
            raise ValueError("estimated cost must be a non-negative integer")
        if not isinstance(self.retry_policy, PartitionRetryPolicy):
            raise TypeError(
                "partition template retry_policy must be a "
                "PartitionRetryPolicy")
        if (self.retry_policy.retry_safe
                and not self.invocation.implementation.retry_safe):
            raise ValueError(
                "automatic retry cannot exceed the implementation contract")
        if self.template_id != self.expected_id():
            raise ValueError("partition task template identity does not verify")

    @classmethod
    def bind(cls, invocation: BoundInvocation, *,
             estimated_cost_units: int | None = None,
             retry_policy: PartitionRetryPolicy | None = None,
             retry_safe: bool | None = None,
             max_attempts: int | None = None) -> "PartitionTaskTemplate":
        """Compile from a resolver output; automatic retry is opt-in."""
        if not isinstance(invocation, BoundInvocation):
            raise TypeError("bind requires a BoundInvocation")
        if estimated_cost_units is None:
            value = invocation.metric_estimates.get("cost_units", 1)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    "bound invocation cost_units must be an integer")
            estimated_cost_units = value
        if retry_policy is not None:
            if retry_safe is not None or max_attempts is not None:
                raise ValueError(
                    "retry_policy cannot be combined with retry_safe or "
                    "max_attempts")
            if not isinstance(retry_policy, PartitionRetryPolicy):
                raise TypeError(
                    "retry_policy must be a PartitionRetryPolicy")
        else:
            safe = False if retry_safe is None else retry_safe
            ceiling = ((3 if safe else 1) if max_attempts is None
                       else max_attempts)
            retry_policy = PartitionRetryPolicy(safe, ceiling)
        payload = cls._payload(
            invocation, estimated_cost_units, retry_policy)
        return cls(strict_hash(payload), invocation, estimated_cost_units,
                   retry_policy)

    @staticmethod
    def _payload(invocation: BoundInvocation, estimated_cost_units: int,
                 retry_policy: PartitionRetryPolicy) -> dict[str, Any]:
        return {
            "schema": "stage8r-partition-task-template-v3",
            "invocation": invocation.to_dict(),
            "estimated_cost_units": estimated_cost_units,
            "retry_policy": retry_policy.to_dict(),
        }

    @property
    def retry_safe(self) -> bool:
        """Compatibility view; the authority is the typed policy."""
        return self.retry_policy.retry_safe

    @property
    def max_attempts(self) -> int:
        return self.retry_policy.max_attempts

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
            self.invocation, self.estimated_cost_units, self.retry_policy))

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
            self.invocation, self.estimated_cost_units, self.retry_policy)
        payload["template_id"] = self.template_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionTaskTemplate":
        raw = require_object_fields(
            value,
            {"schema", "template_id", "invocation",
             "estimated_cost_units", "retry_policy"},
            "PartitionTaskTemplate")
        if raw.pop("schema") != "stage8r-partition-task-template-v3":
            raise ValueError(
                "PartitionTaskTemplate schema is not "
                "stage8r-partition-task-template-v3")
        raw["invocation"] = BoundInvocation.from_dict(raw["invocation"])
        raw["retry_policy"] = PartitionRetryPolicy.from_dict(
            raw["retry_policy"])
        return cls(**raw)


__all__ = ["PartitionRetryPolicy", "PartitionTaskTemplate"]
