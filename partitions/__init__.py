"""Stage-7 lazy partitions, bounded admission, and collection completeness.

One scientific selection is resolved once and reused by every compatible
partition.  The partition space is addressed arithmetically rather than
materialised, admission is a single atomic transaction that cannot skip or
multiply a partition, and controller memory is bounded by watermarks rather
than by total partition count.
"""

from .admission import (
    AdmissionPolicy,
    BoundedAdmissionController,
    TopUpResult,
)
from .manifest import CollectionManifest, CollectionState, CompletionPolicy
from .packet import (
    MemberOutcome,
    PacketAttempt,
    PacketMember,
    PacketResult,
    WorkPacket,
    fuse_members,
)
from .space import AxisKind, PartitionAxis, PartitionKey, PartitionSetSpec
from .store import (
    AdmissionResult,
    CursorConflictError,
    CursorState,
    PartitionStore,
)
from .template import PartitionTaskTemplate

__all__ = [
    "AdmissionPolicy",
    "AdmissionResult",
    "AxisKind",
    "BoundedAdmissionController",
    "CollectionManifest",
    "CollectionState",
    "CompletionPolicy",
    "CursorConflictError",
    "CursorState",
    "MemberOutcome",
    "PacketAttempt",
    "PacketResult",
    "PacketMember",
    "PartitionAxis",
    "PartitionKey",
    "PartitionSetSpec",
    "PartitionStore",
    "PartitionTaskTemplate",
    "TopUpResult",
    "WorkPacket",
    "fuse_members",
]
