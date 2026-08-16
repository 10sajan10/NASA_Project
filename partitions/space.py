"""The deterministic logical partition space, addressed without materializing it.

A partition set is the ordered Cartesian product of declared axes — spatial
tiles, temporal windows, scenarios, ensemble members.  It is represented as a
*mixed-radix number system* rather than a list: partition `n` is decoded from
its index on demand, so a space of 10^6 partitions costs the same resident
memory as one of 10.

This is the structural half of "never materialize all partitions through
``list(...)``".  There is no method here that returns every key, and
:meth:`PartitionSetSpec.iter_keys` yields a bounded window from an explicit
offset so a caller cannot accidentally ask for all of them.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

from capabilities.implementation import _required_text
from engine.runtime.identity import require_object_fields, strict_hash


class AxisKind(str, Enum):
    SPATIAL = "SPATIAL"
    TEMPORAL = "TEMPORAL"
    SCENARIO = "SCENARIO"
    ENSEMBLE = "ENSEMBLE"


@dataclass(frozen=True)
class PartitionAxis:
    """One declared, ordered dimension of the partition space."""

    name: str
    kind: AxisKind
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _required_text(self.name, "partition axis name")
        if not isinstance(self.kind, AxisKind):
            raise TypeError("partition axis kind must be typed")
        if (not isinstance(self.labels, tuple) or not self.labels
                or any(not isinstance(item, str) or not item
                       for item in self.labels)):
            raise TypeError("a partition axis needs a non-empty label tuple")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError(f"axis {self.name!r} repeats a label")

    def __len__(self) -> int:
        return len(self.labels)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind.value,
                "labels": list(self.labels)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionAxis":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "PartitionAxis")
        raw["kind"] = AxisKind(raw["kind"])
        raw["labels"] = tuple(raw["labels"])
        return cls(**raw)


@dataclass(frozen=True)
class PartitionKey:
    """One partition's identity: its index and its per-axis coordinates."""

    index: int
    coordinates: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) \
                or self.index < 0:
            raise ValueError("partition index must be a non-negative integer")
        if (not isinstance(self.coordinates, tuple) or not self.coordinates
                or any(not isinstance(item, tuple) or len(item) != 2
                       for item in self.coordinates)):
            raise TypeError("partition coordinates must be (axis, label) pairs")

    @property
    def label(self) -> str:
        """A stable, human-readable address such as ``tile=t3/time=w0``."""
        return "/".join(f"{axis}={value}" for axis, value in self.coordinates)

    @property
    def key_id(self) -> str:
        return strict_hash({
            "schema": "stage7-partition-key-v1",
            "coordinates": [list(item) for item in self.coordinates],
        })

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index,
                "coordinates": [list(item) for item in self.coordinates]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionKey":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "PartitionKey")
        raw["coordinates"] = tuple(
            (item[0], item[1]) for item in raw["coordinates"])
        return cls(**raw)


@dataclass(frozen=True)
class PartitionSetSpec:
    """A finite partition space addressed by index, never held in memory."""

    set_id: str
    axes: tuple[PartitionAxis, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.axes, tuple) or not self.axes
                or not all(isinstance(item, PartitionAxis) for item in self.axes)):
            raise TypeError("a partition set needs a non-empty axis tuple")
        names = [axis.name for axis in self.axes]
        if len(set(names)) != len(names):
            raise ValueError("partition axes cannot repeat a name")
        if self.set_id != self.expected_id():
            raise ValueError("partition set identity does not verify")

    @classmethod
    def bind(cls, axes: tuple[PartitionAxis, ...]) -> "PartitionSetSpec":
        values = tuple(axes)
        return cls(strict_hash(cls._payload(values)), values)

    @staticmethod
    def _payload(axes: tuple[PartitionAxis, ...]) -> dict[str, Any]:
        # Axis *order* is part of identity: it fixes the enumeration order, and
        # a different order is a different partitioning of the same work.
        return {
            "schema": "stage7-partition-set-v1",
            "axes": [axis.to_dict() for axis in axes],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(self.axes))

    @property
    def total(self) -> int:
        """Partition count, computed arithmetically rather than by counting."""
        count = 1
        for axis in self.axes:
            count *= len(axis)
        return count

    def key_at(self, index: int) -> PartitionKey:
        """Decode one partition from its index by mixed-radix division."""
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("partition index must be an integer")
        if not 0 <= index < self.total:
            raise IndexError(
                f"partition index {index} is outside 0..{self.total - 1}")
        coordinates: list[tuple[str, str]] = []
        remainder = index
        # Last axis varies fastest, so adjacent indices are adjacent in the
        # innermost dimension.  That is what makes tiny-partition fusion group
        # neighbours rather than scattered work.
        for axis in reversed(self.axes):
            remainder, position = divmod(remainder, len(axis))
            coordinates.append((axis.name, axis.labels[position]))
        coordinates.reverse()
        return PartitionKey(index, tuple(coordinates))

    def iter_keys(self, start: int = 0, limit: int | None = None
                  ) -> Iterator[PartitionKey]:
        """Yield a bounded window of partitions from ``start``.

        ``limit`` is required in practice by every caller in this package; the
        default of ``None`` walks to the end and exists only for small tests.
        """
        if start < 0:
            raise ValueError("partition window start cannot be negative")
        if limit is not None and (isinstance(limit, bool)
                                  or not isinstance(limit, int) or limit < 0):
            raise ValueError("partition window limit must be a non-negative int")
        stop = self.total if limit is None else min(self.total, start + limit)
        for index in range(start, stop):
            yield self.key_at(index)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(self.axes)
        payload["set_id"] = self.set_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PartitionSetSpec":
        raw = require_object_fields(
            value, {"schema", "set_id", "axes"}, "PartitionSetSpec")
        if raw.pop("schema") != "stage7-partition-set-v1":
            raise ValueError("PartitionSetSpec schema is not stage7-partition-set-v1")
        raw["axes"] = tuple(PartitionAxis.from_dict(item) for item in raw["axes"])
        return cls(**raw)


__all__ = ["AxisKind", "PartitionAxis", "PartitionKey", "PartitionSetSpec"]
