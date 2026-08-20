"""Automatic native-artifact registration and workflow discovery."""

from .records import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactRecord,
    ArtifactRegistrySnapshot,
    ArtifactSnapshotEntry,
)
from .registry import ArtifactMatch, ArtifactMatchStatus, ArtifactRegistry
from .service import ArtifactWorkflowOutcome, ArtifactWorkflowResolver
from .manifest import WorkflowManifest
from .coordinator import (
    ArtifactOutputEvent,
    ArtifactTargetCoordinator,
    OutputEventStatus,
    TargetRequest,
    TargetState,
    TargetStatus,
)
from .registry import ArtifactQuery, VerificationPolicy
from .runtime import RuntimeArtifactEventBridge
from .publication import (
    NativePublicationDeclaration,
    NativePublicationProposal,
    PublicationProducerRole,
    prepare_native_publication,
)
from .query import (
    ArtifactSnapshotQuery,
    ArtifactSnapshotQueryResult,
    SnapshotArtifactCatalog,
)

__all__ = [
    "ArtifactAvailability",
    "ArtifactInput",
    "ArtifactMatch",
    "ArtifactMatchStatus",
    "ArtifactOutputEvent",
    "ArtifactQuery",
    "ArtifactRecord",
    "ArtifactRegistry",
    "ArtifactRegistrySnapshot",
    "ArtifactSnapshotQuery",
    "ArtifactSnapshotQueryResult",
    "ArtifactSnapshotEntry",
    "ArtifactTargetCoordinator",
    "ArtifactWorkflowOutcome",
    "ArtifactWorkflowResolver",
    "OutputEventStatus",
    "NativePublicationDeclaration",
    "NativePublicationProposal",
    "PublicationProducerRole",
    "RuntimeArtifactEventBridge",
    "SnapshotArtifactCatalog",
    "TargetRequest",
    "TargetState",
    "TargetStatus",
    "VerificationPolicy",
    "WorkflowManifest",
    "prepare_native_publication",
]
