"""Stage-4 explicit semantic transformation layer."""

from .model import (
    TransformationCatalog,
    TransformationKind,
    TransformationPort,
    TransformationSpec,
    UnitAffineRule,
    ValueSemantics,
    exact_requirement,
    unit_affine_rule,
    unit_affine_rules,
)
from .search import (
    ReachableDescriptorState,
    TransformationExpansion,
    TransformationFrontierCode,
    TransformationFrontierItem,
    TransformationLimitCode,
    TransformationLimitReason,
    TransformationSearchLimits,
    TransformationTransition,
    expand_transform_catalog,
)

__all__ = [
    "ReachableDescriptorState",
    "TransformationCatalog",
    "TransformationExpansion",
    "TransformationFrontierCode",
    "TransformationFrontierItem",
    "TransformationKind",
    "TransformationLimitCode",
    "TransformationLimitReason",
    "TransformationPort",
    "TransformationSearchLimits",
    "TransformationSpec",
    "TransformationTransition",
    "UnitAffineRule",
    "ValueSemantics",
    "exact_requirement",
    "expand_transform_catalog",
    "unit_affine_rule",
    "unit_affine_rules",
]
