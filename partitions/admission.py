"""Bounded admission: watermarks, windows, and a memory ceiling that holds.

Nothing here scales with total partition count.  The controller admits work
only when in-flight partitions fall to the low watermark, tops up to the high
watermark one bounded window at a time, and streams the admitted set back in
bounded slices.  A 10-partition run and a 10^6-partition run therefore occupy
the same resident memory; only the durable store grows.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from engine.runtime.identity import require_object_fields

from .manifest import CollectionManifest, CollectionState
from .packet import (
    PacketMember,
    WorkPacket,
    expected_deployment_binding_id,
    fuse_members,
)
from .space import PartitionSetSpec
from .store import PartitionStore
from .template import PartitionTaskTemplate


@dataclass(frozen=True)
class AdmissionPolicy:
    """The only knobs that decide controller memory."""

    window_size: int = 256
    low_watermark: int = 128
    high_watermark: int = 512
    max_packet_members: int = 32
    target_packet_cost: int = 16

    def __post_init__(self) -> None:
        for name in ("window_size", "low_watermark", "high_watermark",
                     "max_packet_members", "target_packet_cost"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.low_watermark > self.high_watermark:
            raise ValueError("low watermark cannot exceed the high watermark")

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AdmissionPolicy":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AdmissionPolicy")
        return cls(**raw)


@dataclass(frozen=True)
class TopUpResult:
    windows: int
    admitted: int
    in_flight: int
    exhausted: bool


class BoundedAdmissionController:
    """Drives admission for one collection without materialising it."""

    def __init__(self, store: PartitionStore, manifest: CollectionManifest,
                 spec: PartitionSetSpec, template: PartitionTaskTemplate, *,
                 policy: AdmissionPolicy = AdmissionPolicy()) -> None:
        if not isinstance(store, PartitionStore):
            raise TypeError("a bounded controller needs a PartitionStore")
        if not isinstance(policy, AdmissionPolicy):
            raise TypeError("policy must be an AdmissionPolicy")
        self.store = store
        self.manifest = manifest
        self.spec = spec
        self.template = template
        self.policy = policy
        store.open_collection(manifest, spec, template)

    @property
    def collection_id(self) -> str:
        return self.manifest.collection_id

    def top_up(self, *, fault: Callable[[str], None] | None = None
               ) -> TopUpResult:
        """Admit windows until in-flight reaches the high watermark.

        Called when in-flight has fallen to the low watermark.  Each window is
        its own atomic transaction, so an interruption part way through a
        top-up leaves a consistent cursor and simply resumes on the next call.
        """
        in_flight = self.store.in_flight(self.collection_id)
        if in_flight > self.policy.low_watermark:
            return TopUpResult(0, 0, in_flight, self._exhausted())

        windows = 0
        admitted = 0
        while in_flight < self.policy.high_watermark:
            if self._exhausted():
                break
            remaining = self.policy.high_watermark - in_flight
            result = self.store.admit_window(
                self.collection_id, self.spec, self.template,
                min(self.policy.window_size, remaining), fault=fault)
            if result.admitted == 0:
                break
            windows += 1
            admitted += result.admitted
            in_flight += result.admitted
        return TopUpResult(windows, admitted, in_flight, self._exhausted())

    def _exhausted(self) -> bool:
        return self.store.cursor(self.collection_id).next_index >= self.spec.total

    def next_packets(self, *, limit: int | None = None
                     ) -> tuple[WorkPacket, ...]:
        """Bundle a bounded slice of admitted partitions into work packets."""
        ceiling = limit if limit is not None else self.policy.high_watermark
        binding_id = expected_deployment_binding_id(
            self.collection_id, self.template.template_id)
        members = tuple(
            PacketMember(
                logical_task_key=key, partition_index=index,
                deployment_binding_id=binding_id)
            for key, index in self.store.iter_admitted(
                self.collection_id, ceiling))
        if not members:
            return ()
        return fuse_members(
            members, self.collection_id, self.template.template_id,
            max_members=self.policy.max_packet_members,
            cost_per_member=self.template.estimated_cost_units,
            target_packet_cost=self.policy.target_packet_cost)

    def state(self) -> CollectionState:
        return self.store.state(self.collection_id)

    def is_complete(self) -> bool:
        return self.state().satisfies(self.manifest)

    def drain(self, *, fault: Callable[[str], None] | None = None
              ) -> Iterator[tuple[WorkPacket, ...]]:
        """Yield successive bounded batches of packets until the space is done.

        This is the whole scalable execution loop: top up, hand out a bounded
        batch, and repeat.  It never holds more than the high watermark.
        """
        while True:
            self.top_up(fault=fault)
            packets = self.next_packets()
            if not packets:
                if self._exhausted():
                    return
                raise RuntimeError(
                    "admission stalled with partitions still unadmitted")
            yield packets


__all__ = [
    "AdmissionPolicy",
    "BoundedAdmissionController",
    "TopUpResult",
]
