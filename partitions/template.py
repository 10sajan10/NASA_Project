"""One scientific selection, shared by every partition that is compatible.

The template is where "resolve once, execute many" becomes structural.  It
holds the *single* bound invocation the resolver selected, and every partition
derives its logical task key from that same invocation.  Partitions cannot
drift onto different producers, because there is only one to drift from.

Logical task identity follows Section 8.7 exactly:

```text
bound invocation/subgraph hash
    + operation and ordered prospective input slot IDs
    + task-template ID
    + partition key
```

Input *slot* IDs rather than content digests, because generated inputs have no
content digest at compile time.  Commit later binds those slots to digests
without renaming any task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)

from .space import PartitionKey


@dataclass(frozen=True)
class PartitionTaskTemplate:
    """The operation shared by every partition of one collection."""

    template_id: str
    invocation_key: str
    operation_key: str
    input_slot_ids: tuple[str, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    estimated_cost_units: int = 1
    retry_safe: bool = True

    def __post_init__(self) -> None:
        _digest(self.template_id, "template_id")
        _required_text(self.invocation_key, "template invocation_key")
        _required_text(self.operation_key, "template operation_key")
        if (not isinstance(self.input_slot_ids, tuple)
                or any(not isinstance(item, str) or not item
                       for item in self.input_slot_ids)):
            raise TypeError("input slot IDs must be a text tuple")
        object.__setattr__(self, "parameters", freeze_json(
            strict_copy(dict(self.parameters))))
        if (isinstance(self.estimated_cost_units, bool)
                or not isinstance(self.estimated_cost_units, int)
                or self.estimated_cost_units < 0):
            raise ValueError("estimated cost must be a non-negative integer")
        if type(self.retry_safe) is not bool:
            raise TypeError("retry_safe must be bool")
        if self.template_id != self.expected_id():
            raise ValueError("partition task template identity does not verify")

    @classmethod
    def bind(cls, *, invocation_key: str, operation_key: str,
             input_slot_ids: tuple[str, ...] = (),
             parameters: dict[str, Any] | None = None,
             estimated_cost_units: int = 1,
             retry_safe: bool = True) -> "PartitionTaskTemplate":
        values = dict(parameters or {})
        payload = cls._payload(invocation_key, operation_key,
                               tuple(input_slot_ids), values,
                               estimated_cost_units, retry_safe)
        return cls(strict_hash(payload), invocation_key, operation_key,
                   tuple(input_slot_ids), values, estimated_cost_units,
                   retry_safe)

    @staticmethod
    def _payload(invocation_key: str, operation_key: str,
                 input_slot_ids: tuple[str, ...], parameters: dict[str, Any],
                 estimated_cost_units: int, retry_safe: bool) -> dict[str, Any]:
        return {
            "schema": "stage7-partition-task-template-v1",
            "invocation_key": invocation_key,
            "operation_key": operation_key,
            "input_slot_ids": list(input_slot_ids),
            "parameters": strict_copy(parameters),
            "estimated_cost_units": estimated_cost_units,
            "retry_safe": retry_safe,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.invocation_key, self.operation_key, self.input_slot_ids,
            dict(self.parameters), self.estimated_cost_units, self.retry_safe))

    def logical_task_key(self, partition: PartitionKey) -> str:
        """The stable identity of this template applied to one partition.

        Deliberately independent of deployment, resources, and attempt number:
        revising one task's resource envelope must not rename every unaffected
        logical task.
        """
        if not isinstance(partition, PartitionKey):
            raise TypeError("logical_task_key requires a PartitionKey")
        return strict_hash({
            "schema": "stage7-logical-task-key-v1",
            "invocation_key": self.invocation_key,
            "operation_key": self.operation_key,
            "input_slot_ids": list(self.input_slot_ids),
            "template_id": self.template_id,
            "partition_key_id": partition.key_id,
        })

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.invocation_key, self.operation_key, self.input_slot_ids,
            dict(self.parameters), self.estimated_cost_units, self.retry_safe)
        payload["template_id"] = self.template_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionTaskTemplate":
        raw = require_object_fields(
            value,
            {"schema", "template_id", "invocation_key", "operation_key",
             "input_slot_ids", "parameters", "estimated_cost_units",
             "retry_safe"},
            "PartitionTaskTemplate")
        if raw.pop("schema") != "stage7-partition-task-template-v1":
            raise ValueError(
                "PartitionTaskTemplate schema is not "
                "stage7-partition-task-template-v1")
        raw["input_slot_ids"] = tuple(raw["input_slot_ids"])
        return cls(**raw)


__all__ = ["PartitionTaskTemplate"]
