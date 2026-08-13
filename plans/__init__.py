"""Immutable scientific derivation-plan records.

The plan layer is deliberately independent of any optimizer.  Stage 2's
exhaustive oracle and later exact/approximate selectors must emit the same
records and pass the same identity checks.
"""

from .types import (
    ArtifactLeafBinding,
    BoundInvocationBinding,
    BoundDerivationPlan,
    CandidateDerivationPlan,
    CompatibilityProofRecord,
    DeploymentPlan,
    InvocationDeploymentBinding,
    PlanSnapshotRef,
    ProducerKind,
    ProducerOutputRef,
    SatisfactionBinding,
    SatisfactionKind,
)

__all__ = [
    "ArtifactLeafBinding",
    "BoundInvocationBinding",
    "BoundDerivationPlan",
    "CandidateDerivationPlan",
    "CompatibilityProofRecord",
    "DeploymentPlan",
    "InvocationDeploymentBinding",
    "PlanSnapshotRef",
    "ProducerKind",
    "ProducerOutputRef",
    "SatisfactionBinding",
    "SatisfactionKind",
]
