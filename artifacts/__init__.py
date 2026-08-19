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
    "ArtifactSnapshotEntry",
    "ArtifactTargetCoordinator",
    "ArtifactWorkflowOutcome",
    "ArtifactWorkflowResolver",
    "OutputEventStatus",
    "RuntimeArtifactEventBridge",
    "TargetRequest",
    "TargetState",
    "TargetStatus",
    "VerificationPolicy",
    "WorkflowManifest",
]
