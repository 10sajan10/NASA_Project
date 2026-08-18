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
from .discovery import (
    CatalogDiscoveryProvenance,
    DiscoveryLayerCertificate,
    DiscoveryLimitActivation,
    DiscoveryLimitSetting,
)
from .implementation import ImplementationRef
from .model import (
    AcquisitionAuthority,
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
    TransformationAuthority,
    artifact_evidence_subject,
    invocation_evidence_subject,
)
from .parameters import ParameterField, ParameterKind, ParameterSchema

__all__ = [
    "AcquisitionAuthority",
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
    "CatalogDiscoveryProvenance",
    "DeploymentCapabilitySnapshot",
    "DeploymentFeasibilityProof",
    "DeploymentRejection",
    "DeploymentRejectionCode",
    "DiscoveryLayerCertificate",
    "DiscoveryLimitActivation",
    "DiscoveryLimitSetting",
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
    "TransformationAuthority",
    "binder_keys",
    "binder_rule",
    "artifact_evidence_subject",
    "invocation_evidence_subject",
]
