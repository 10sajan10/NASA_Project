"""Lower a bound manifest to an ordinary Stage-2 capability.

This is the same trick Stage 4 used for transformations, for the same reason.
Acquisition does not get a private code path inside the resolver: a bound
manifest becomes a normal costed :class:`CapabilitySpec` with a closed binder,
and it then competes against models, transformations, and other sources inside
one global selection.

The manifest root travels as a *scientific parameter* of that capability, so it
lands in the invocation identity, the candidate plan, and therefore the bound
derivation ID.  Changing which bytes a plan reads necessarily changes the plan.
"""
from __future__ import annotations

from capabilities import (
    BinderRef,
    BindingParameterization,
    CapabilitySpec,
    DescriptorTemplate,
    ExecutionProfile,
    ParameterField,
    ParameterKind,
    ParameterSchema,
)
from contracts import ArtifactDescriptor, MissingnessStatus

from .binding import BoundAssetManifest

ACQUISITION_BINDER_KEY = "acquisition.materialize.bind.v1"
ACQUISITION_OPERATION_KEY = "acquisition.materialize.v1"


def acquisition_capability_id(bound: BoundAssetManifest) -> str:
    """A stable, readable capability identity for one exact binding."""
    return f"acquire:{bound.source_id}:{bound.manifest_root[:16]}"


def lower_manifest_to_capability(
    bound: BoundAssetManifest,
    descriptor: ArtifactDescriptor,
    execution_profile: ExecutionProfile,
    *,
    cost_units: int,
    capability_version: str = "1.0.0",
    evidence_profile_id: str = "evidence:unknown",
) -> CapabilitySpec:
    """Turn one bound manifest into a producer the selector can choose.

    The descriptor must agree with the query that produced the manifest.  A
    manifest bound while asking for one concept cannot be relabelled as another
    on the way into the catalog, which is the acquisition-layer equivalent of
    Stage 4's refusal to let a conversion forge its output metadata.
    """
    if not isinstance(bound, BoundAssetManifest):
        raise TypeError("lowering requires a BoundAssetManifest")
    if not isinstance(descriptor, ArtifactDescriptor):
        raise TypeError("lowering requires an ArtifactDescriptor")
    if not isinstance(execution_profile, ExecutionProfile):
        raise TypeError("lowering requires an ExecutionProfile")
    if isinstance(cost_units, bool) or not isinstance(cost_units, int) \
            or cost_units < 0:
        raise ValueError("acquisition cost_units must be a non-negative integer")

    query = bound.query_payload
    for field, value, label in (
            ("concept_id", descriptor.concept_id, "concept"),
            ("schema_version", descriptor.schema_version, "schema version"),
            ("units", descriptor.units, "units"),
            ("representation", descriptor.representation, "representation")):
        if query.get(field) != value:
            raise ValueError(
                f"acquired descriptor {label} {value!r} does not match the "
                f"bound query value {query.get(field)!r}")
    if descriptor.missingness.status is MissingnessStatus.COMPLETE and \
            not bound.coverage.complete:
        raise ValueError(
            "an artifact cannot declare COMPLETE missingness over a gap")
    if execution_profile.implementation.operation_key != ACQUISITION_OPERATION_KEY:
        raise ValueError(
            "acquisition capabilities must use the closed materialize operation")

    return CapabilitySpec.bind(
        capability_id=acquisition_capability_id(bound),
        capability_version=capability_version,
        implementation=execution_profile.implementation,
        binder=BinderRef.from_key(ACQUISITION_BINDER_KEY),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("asset_ids", ParameterKind.JSON),
            ParameterField("manifest_root", ParameterKind.STRING),
        )),
        parameterizations=(BindingParameterization(
            {
                "manifest_root": bound.manifest_root,
                "asset_ids": list(bound.asset_ids),
            },
            {"cost_units": cost_units},
        ),),
        execution_profile_id=execution_profile.profile_id,
        evidence_profile_id=evidence_profile_id,
    )


__all__ = [
    "ACQUISITION_BINDER_KEY",
    "ACQUISITION_OPERATION_KEY",
    "acquisition_capability_id",
    "lower_manifest_to_capability",
]
