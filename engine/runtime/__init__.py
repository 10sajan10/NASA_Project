"""Stage-1 durable local execution kernel.

This package is intentionally independent from scientific resolution.  It
executes an already-bound graph, supervises local subprocess attempts, and
publishes only validated, fenced artifacts.
"""

from .types import (
    ArtifactRecipe,
    AttemptInputReceipt,
    AttemptSpec,
    AttemptState,
    BoundExecutionGraph,
    BoundTask,
    ExecutableComponent,
    ExternalArtifactInputBinding,
    ExternalHandle,
    InputBinding,
    OutputSpec,
    ProviderObservation,
    ResourceRequest,
    RunState,
    ScientificArtifactBinding,
    SiteSnapshot,
    TaskState,
    TaskTemplate,
    WakeKind,
)

# The controller/provider imports are intentionally after the identity types:
# worker processes can import the closed data model without initializing any
# controller state.
from .controller import WorkflowController
from .provider import LocalSubprocessProvider, SubmissionOutcomeUnknown

__all__ = [
    "ArtifactRecipe",
    "AttemptInputReceipt",
    "AttemptSpec",
    "AttemptState",
    "BoundExecutionGraph",
    "BoundTask",
    "ExecutableComponent",
    "ExternalArtifactInputBinding",
    "ExternalHandle",
    "InputBinding",
    "OutputSpec",
    "ProviderObservation",
    "ResourceRequest",
    "RunState",
    "ScientificArtifactBinding",
    "SiteSnapshot",
    "TaskState",
    "TaskTemplate",
    "WakeKind",
    "WorkflowController",
    "LocalSubprocessProvider",
    "SubmissionOutcomeUnknown",
]
