"""Bundling many tiny partitions into one submission, without merging identity.

Scheduling overhead per partition is fixed, so a space of 10,000 two-second
partitions spends most of its time in the controller rather than in science.
Fusion bundles neighbours into one provider submission to amortise that.

What fusion must **not** do is merge logical identity.  A :class:`WorkPacket`
owns one provider handle and an *ordered set of members*; validation and commit
stay per-partition; and a partially failed packet keeps its committed members
and retries only the uncommitted retry-safe ones.  Losing that would turn one
bad partition into 256 recomputed ones and would destroy per-partition lineage.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import require_object_fields, strict_hash


class MemberOutcome(str, Enum):
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


@dataclass(frozen=True)
class PacketMember:
    """One partition inside a packet, retaining its own identity."""

    logical_task_key: str
    partition_index: int
    deployment_binding_id: str

    def __post_init__(self) -> None:
        _digest(self.logical_task_key, "member logical_task_key")
        if (isinstance(self.partition_index, bool)
                or not isinstance(self.partition_index, int)
                or self.partition_index < 0):
            raise ValueError("member partition index must be non-negative")
        _required_text(self.deployment_binding_id, "deployment_binding_id")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PacketMember":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "PacketMember")
        return cls(**raw)


@dataclass(frozen=True)
class WorkPacket:
    """One provider submission covering an ordered set of partitions."""

    packet_id: str
    template_id: str
    members: tuple[PacketMember, ...]

    def __post_init__(self) -> None:
        _digest(self.packet_id, "packet_id")
        _digest(self.template_id, "packet template_id")
        if (not isinstance(self.members, tuple) or not self.members
                or not all(isinstance(item, PacketMember)
                           for item in self.members)):
            raise TypeError("a packet needs a non-empty member tuple")
        keys = [item.logical_task_key for item in self.members]
        if len(set(keys)) != len(keys):
            raise ValueError("a packet cannot repeat a logical task")
        if [item.partition_index for item in self.members] != sorted(
                item.partition_index for item in self.members):
            raise ValueError("packet members must be in partition order")
        if self.packet_id != self.expected_id():
            raise ValueError("work packet identity does not verify")

    @classmethod
    def bind(cls, template_id: str,
             members: Iterable[PacketMember]) -> "WorkPacket":
        values = tuple(members)
        return cls(strict_hash(cls._payload(template_id, values)),
                   template_id, values)

    @staticmethod
    def _payload(template_id: str,
                 members: tuple[PacketMember, ...]) -> dict[str, Any]:
        return {
            "schema": "stage7-work-packet-v1",
            "template_id": template_id,
            "members": [item.to_dict() for item in members],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(self.template_id, self.members))

    @property
    def logical_task_keys(self) -> tuple[str, ...]:
        return tuple(item.logical_task_key for item in self.members)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(self.template_id, self.members)
        payload["packet_id"] = self.packet_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkPacket":
        raw = require_object_fields(
            value, {"schema", "packet_id", "template_id", "members"},
            "WorkPacket")
        if raw.pop("schema") != "stage7-work-packet-v1":
            raise ValueError("WorkPacket schema is not stage7-work-packet-v1")
        raw["members"] = tuple(
            PacketMember.from_dict(item) for item in raw["members"])
        return cls(**raw)


@dataclass(frozen=True)
class PacketAttempt:
    """Identity minted *before* a provider submission starts.

    Outcomes cannot be part of attempt identity because they do not exist at
    submission time. ``fence_token`` distinguishes leases/restarts and lets a
    durable controller reject a late result from an earlier worker.
    """

    attempt_id: str
    packet_id: str
    attempt_number: int
    fence_token: str

    def __post_init__(self) -> None:
        _digest(self.attempt_id, "packet attempt_id")
        _digest(self.packet_id, "attempt packet_id")
        if (isinstance(self.attempt_number, bool)
                or not isinstance(self.attempt_number, int)
                or self.attempt_number < 1):
            raise ValueError("attempt number must be a positive integer")
        _required_text(self.fence_token, "attempt fence_token")
        if self.attempt_id != self.expected_id():
            raise ValueError("packet attempt identity does not verify")

    @classmethod
    def bind(cls, packet: WorkPacket, attempt_number: int, *,
             fence_token: str) -> "PacketAttempt":
        payload = cls._payload(packet.packet_id, attempt_number, fence_token)
        return cls(strict_hash(payload), packet.packet_id, attempt_number,
                   fence_token)

    @staticmethod
    def _payload(packet_id: str, attempt_number: int,
                 fence_token: str) -> dict[str, Any]:
        return {
            "schema": "stage7-packet-attempt-v2",
            "packet_id": packet_id,
            "attempt_number": attempt_number,
            "fence_token": fence_token,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.packet_id, self.attempt_number, self.fence_token))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage7-packet-attempt-v2",
            "attempt_id": self.attempt_id,
            "packet_id": self.packet_id,
            "attempt_number": self.attempt_number,
            "fence_token": self.fence_token,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PacketAttempt":
        raw = require_object_fields(
            value,
            {"schema", "attempt_id", "packet_id", "attempt_number",
             "fence_token"},
            "PacketAttempt")
        if raw.pop("schema") != "stage7-packet-attempt-v2":
            raise ValueError("PacketAttempt schema is not stage7-packet-attempt-v2")
        return cls(**raw)


@dataclass(frozen=True)
class PacketResult:
    """A separately identified result for one pre-existing packet attempt."""

    result_id: str
    attempt_id: str
    packet_id: str
    outcomes: tuple[tuple[str, MemberOutcome], ...]

    def __post_init__(self) -> None:
        _digest(self.result_id, "packet result_id")
        _digest(self.attempt_id, "packet result attempt_id")
        _digest(self.packet_id, "packet result packet_id")
        if not isinstance(self.outcomes, tuple) or not self.outcomes:
            raise ValueError("a packet result must report every member")
        for entry in self.outcomes:
            if (not isinstance(entry, tuple) or len(entry) != 2
                    or not isinstance(entry[0], str) or not entry[0]
                    or not isinstance(entry[1], MemberOutcome)):
                raise TypeError(
                    "outcomes must be (logical_task_key, MemberOutcome) pairs")
        keys = [item[0] for item in self.outcomes]
        if len(set(keys)) != len(keys):
            raise ValueError("a packet result cannot report a member twice")
        if self.result_id != self.expected_id():
            raise ValueError("packet result identity does not verify")

    @classmethod
    def bind(cls, attempt: PacketAttempt, packet: WorkPacket,
             outcomes: Iterable[tuple[str, MemberOutcome]]) -> "PacketResult":
        if attempt.packet_id != packet.packet_id:
            raise ValueError("packet attempt does not belong to this packet")
        values = tuple(outcomes)
        if {item[0] for item in values} != set(packet.logical_task_keys):
            raise ValueError(
                "a packet result must report exactly its packet's members")
        payload = cls._payload(attempt.attempt_id, packet.packet_id, values)
        return cls(strict_hash(payload), attempt.attempt_id,
                   packet.packet_id, values)

    @staticmethod
    def _payload(attempt_id: str, packet_id: str,
                 outcomes: tuple[tuple[str, MemberOutcome], ...]
                 ) -> dict[str, Any]:
        return {
            "schema": "stage7-packet-result-v1",
            "attempt_id": attempt_id,
            "packet_id": packet_id,
            "outcomes": [[key, outcome.value] for key, outcome in outcomes],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.attempt_id, self.packet_id, self.outcomes))

    def outcome_for(self, logical_task_key: str) -> MemberOutcome:
        for key, outcome in self.outcomes:
            if key == logical_task_key:
                return outcome
        raise KeyError(f"{logical_task_key!r} is not in this result")

    def committed_keys(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, outcome in self.outcomes
                            if outcome is MemberOutcome.COMMITTED))

    def retryable_keys(self, *, retry_safe: bool = False) -> tuple[str, ...]:
        if not retry_safe:
            return ()
        return tuple(sorted(
            key for key, outcome in self.outcomes
            if outcome in (MemberOutcome.FAILED, MemberOutcome.NOT_ATTEMPTED)))

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(self.attempt_id, self.packet_id, self.outcomes)
        payload["result_id"] = self.result_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PacketResult":
        raw = require_object_fields(
            value,
            {"schema", "result_id", "attempt_id", "packet_id", "outcomes"},
            "PacketResult")
        if raw.pop("schema") != "stage7-packet-result-v1":
            raise ValueError("PacketResult schema is not stage7-packet-result-v1")
        if not isinstance(raw["outcomes"], list):
            raise ValueError("PacketResult.outcomes must be an array")
        raw["outcomes"] = tuple(
            (item[0], MemberOutcome(item[1])) for item in raw["outcomes"])
        return cls(**raw)


def fuse_members(members: Iterable[PacketMember], template_id: str, *,
                 max_members: int, cost_per_member: int,
                 target_packet_cost: int) -> tuple[WorkPacket, ...]:
    """Group adjacent partitions into packets, keeping per-partition lineage.

    Fusion is bounded twice over: by ``max_members`` and by a target cost, so a
    packet neither grows unboundedly nor bundles work that was already large
    enough to submit on its own.
    """
    if max_members < 1:
        raise ValueError("max_members must be positive")
    if cost_per_member < 0 or target_packet_cost < 0:
        raise ValueError("fusion costs must be non-negative")
    if cost_per_member >= target_packet_cost:
        group_size = 1          # already big enough; do not bundle
    else:
        by_cost = (target_packet_cost // cost_per_member
                   if cost_per_member else max_members)
        group_size = max(1, min(max_members, by_cost))
    ordered = sorted(members, key=lambda item: item.partition_index)
    packets: list[WorkPacket] = []
    for start in range(0, len(ordered), group_size):
        packets.append(WorkPacket.bind(
            template_id, tuple(ordered[start:start + group_size])))
    return tuple(packets)


__all__ = [
    "MemberOutcome",
    "PacketAttempt",
    "PacketResult",
    "PacketMember",
    "WorkPacket",
    "fuse_members",
]
