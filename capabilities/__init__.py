"""Strict Stage-2 scientific capability and static deployment layer."""

from .binders import BinderRef, binder_keys, binder_rule
from .catalog import CapabilityCatalog
from .deployment import (
    DeploymentCapabilitySnapshot,
    DeploymentFeasibilityProof,
    DeploymentRejection,
    DeploymentRejectionCode,
    ExecutionProfile,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
    SiteFeasibility,
)
from .implementation import ImplementationRef
from .model import (
    ArtifactLeaf,
    BindingCandidate,
    BindingEnumeration,
    BindingParameterization,
    BindingRejection,
    BindingRejectionCode,
    BoundInvocation,
    BoundOutputPort,
    CapabilitySpec,
    DescriptorTemplate,
    InputPortTemplate,
    artifact_evidence_subject,
    invocation_evidence_subject,
)
from .parameters import ParameterField, ParameterKind, ParameterSchema

__all__ = [
    "ArtifactLeaf",
    "BinderRef",
    "BindingCandidate",
    "BindingEnumeration",
    "BindingParameterization",
    "BindingRejection",
    "BindingRejectionCode",
    "BoundInvocation",
    "BoundOutputPort",
    "CapabilityCatalog",
    "CapabilitySpec",
    "DeploymentCapabilitySnapshot",
    "DeploymentFeasibilityProof",
    "DeploymentRejection",
    "DeploymentRejectionCode",
    "DescriptorTemplate",
    "ExecutionProfile",
    "ImplementationRef",
    "InputPortTemplate",
    "ParameterField",
    "ParameterKind",
    "ParameterSchema",
    "PlacementRequirements",
    "ResourceEnvelope",
    "SiteClassCapability",
    "SiteFeasibility",
    "binder_keys",
    "binder_rule",
    "artifact_evidence_subject",
    "invocation_evidence_subject",
]
