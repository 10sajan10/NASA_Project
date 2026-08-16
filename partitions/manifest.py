"""Collection completeness: a partial result never passes as a whole one.

The failure this module exists to prevent is quiet and expensive: 9,998 of
10,000 partitions commit, two fail, and the collection reports success anyway.
Every completion policy here is explicit, and the default one — ``ALL`` —
requires every expected partition committed and none failed.

Counts live in the durable store because they change; this module holds the
frozen *policy* and the pure predicate that judges a set of counts against it.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from capabilities.implementation import _digest
from contracts.identity import canonical_decimal, decimal_value
from engine.runtime.identity import require_object_fields, strict_hash


class CompletionPolicy(str, Enum):
    """How much of a partition space must commit before it counts as done."""

    ALL = "ALL"
    AT_LEAST = "AT_LEAST"
    FRACTION = "FRACTION"


@dataclass(frozen=True)
class CollectionManifest:
    """The expected partition space and the rule for calling it complete."""

    collection_id: str
    set_id: str
    template_id: str
    expected: int
    policy: CompletionPolicy = CompletionPolicy.ALL
    minimum_committed: int | None = None
    minimum_fraction: str | None = None

    def __post_init__(self) -> None:
        _digest(self.collection_id, "collection_id")
        _digest(self.set_id, "collection set_id")
        _digest(self.template_id, "collection template_id")
        if (isinstance(self.expected, bool)
                or not isinstance(self.expected, int) or self.expected < 1):
            raise ValueError("a collection must expect at least one partition")
        if not isinstance(self.policy, CompletionPolicy):
            raise TypeError("completion policy must be typed")
        if self.policy is CompletionPolicy.AT_LEAST:
            if (self.minimum_committed is None
                    or isinstance(self.minimum_committed, bool)
                    or not isinstance(self.minimum_committed, int)
                    or not 1 <= self.minimum_committed <= self.expected):
                raise ValueError(
                    "AT_LEAST requires a minimum within 1..expected")
            if self.minimum_fraction is not None:
                raise ValueError("AT_LEAST does not take a fraction")
        elif self.policy is CompletionPolicy.FRACTION:
            if self.minimum_fraction is None:
                raise ValueError("FRACTION requires a minimum fraction")
            fraction = canonical_decimal(
                self.minimum_fraction, "minimum fraction")
            if not Decimal("0") < decimal_value(fraction) <= Decimal("1"):
                raise ValueError("minimum fraction must be in (0, 1]")
            object.__setattr__(self, "minimum_fraction", fraction)
            if self.minimum_committed is not None:
                raise ValueError("FRACTION does not take a minimum count")
        elif (self.minimum_committed is not None
                or self.minimum_fraction is not None):
            raise ValueError("ALL takes no threshold")
        if self.collection_id != self.expected_id():
            raise ValueError("collection manifest identity does not verify")

    @classmethod
    def bind(cls, *, set_id: str, template_id: str, expected: int,
             policy: CompletionPolicy = CompletionPolicy.ALL,
             minimum_committed: int | None = None,
             minimum_fraction: str | None = None) -> "CollectionManifest":
        payload = cls._payload(set_id, template_id, expected, policy,
                               minimum_committed, minimum_fraction)
        return cls(strict_hash(payload), set_id, template_id, expected, policy,
                   minimum_committed, minimum_fraction)

    @staticmethod
    def _payload(set_id: str, template_id: str, expected: int,
                 policy: CompletionPolicy, minimum_committed: int | None,
                 minimum_fraction: str | None) -> dict[str, Any]:
        return {
            "schema": "stage7-collection-manifest-v1",
            "set_id": set_id,
            "template_id": template_id,
            "expected": expected,
            "policy": policy.value,
            "minimum_committed": minimum_committed,
            "minimum_fraction": minimum_fraction,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.set_id, self.template_id, self.expected, self.policy,
            self.minimum_committed, self.minimum_fraction))

    @property
    def required_committed(self) -> int:
        """The committed count this policy demands, as a whole number."""
        if self.policy is CompletionPolicy.ALL:
            return self.expected
        if self.policy is CompletionPolicy.AT_LEAST:
            assert self.minimum_committed is not None
            return self.minimum_committed
        assert self.minimum_fraction is not None
        needed = decimal_value(self.minimum_fraction) * Decimal(self.expected)
        # Round up: a fraction policy is a floor, never a rounding-down excuse.
        whole = int(needed)
        return whole if Decimal(whole) == needed else whole + 1

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.set_id, self.template_id, self.expected, self.policy,
            self.minimum_committed, self.minimum_fraction)
        payload["collection_id"] = self.collection_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CollectionManifest":
        raw = require_object_fields(
            value,
            {"schema", "collection_id", "set_id", "template_id", "expected",
             "policy", "minimum_committed", "minimum_fraction"},
            "CollectionManifest")
        if raw.pop("schema") != "stage7-collection-manifest-v1":
            raise ValueError(
                "CollectionManifest schema is not stage7-collection-manifest-v1")
        raw["policy"] = CompletionPolicy(raw["policy"])
        return cls(**raw)


@dataclass(frozen=True)
class CollectionState:
    """Observed counts for one collection, judged against its policy."""

    expected: int
    admitted: int
    committed: int
    failed: int

    def __post_init__(self) -> None:
        for name in ("expected", "admitted", "committed", "failed"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"collection {name} must be a non-negative int")
        if self.admitted > self.expected:
            raise ValueError("more partitions admitted than the space contains")
        if self.committed + self.failed > self.admitted:
            raise ValueError("more partitions resolved than were admitted")

    @property
    def outstanding(self) -> int:
        return self.expected - self.committed - self.failed

    def satisfies(self, manifest: CollectionManifest) -> bool:
        """Whether these counts complete the collection under its policy.

        ``ALL`` additionally requires zero failures: a space where one
        partition failed is not complete no matter how the rest went.
        """
        if not isinstance(manifest, CollectionManifest):
            raise TypeError("satisfies requires a CollectionManifest")
        if self.expected != manifest.expected:
            raise ValueError("state describes a different partition space")
        if manifest.policy is CompletionPolicy.ALL:
            return self.committed == self.expected and self.failed == 0
        return self.committed >= manifest.required_committed

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)


__all__ = ["CollectionManifest", "CollectionState", "CompletionPolicy"]
