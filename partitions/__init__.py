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
from .execution import (
    PartitionNotExecutable,
    compile_packet,
    execute_packet,
)
from .manifest import CollectionManifest, CollectionState, CompletionPolicy
from .inputs import (
    PartitionArtifactInput,
    PartitionInputAssignment,
    PartitionInputManifest,
)
from .packet import (
    MemberOutcome,
    PacketAttempt,
    PacketMember,
    PacketResult,
    WorkPacket,
    expected_deployment_binding_id,
    fuse_members,
)
from .space import AxisKind, PartitionAxis, PartitionKey, PartitionSetSpec
from .store import (
    AdmissionResult,
    RetryDecision,
    CursorConflictError,
    CursorState,
    PacketAttemptAuthorityError,
    PacketAttemptConflictError,
    PacketResultConflictError,
    PartitionStore,
)
from .template import PartitionRetryPolicy, PartitionTaskTemplate

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
    "PacketAttemptAuthorityError",
    "PacketAttemptConflictError",
    "PacketResult",
    "PacketResultConflictError",
    "PacketMember",
    "PartitionNotExecutable",
    "PartitionAxis",
    "PartitionArtifactInput",
    "PartitionInputAssignment",
    "PartitionInputManifest",
    "PartitionKey",
    "PartitionSetSpec",
    "PartitionStore",
    "PartitionRetryPolicy",
    "PartitionTaskTemplate",
    "RetryDecision",
    "TopUpResult",
    "WorkPacket",
    "compile_packet",
    "execute_packet",
    "expected_deployment_binding_id",
    "fuse_members",
]
