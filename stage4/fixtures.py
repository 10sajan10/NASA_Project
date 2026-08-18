"""Domain-neutral Stage-4 transformation fixtures.

The concepts here are meaningless scalar and vector labels.  Stage 4 must show
that an explicit transformation can *compete with direct data on cost* without
the resolver knowing what the quantity means.  Wind, terrain, and every other
scientific domain enter through exactly these interfaces; none is special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass

from capabilities import (
    BinderRef,
    BindingParameterization,
    CapabilityCatalog,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    ExecutionProfile,
    ImplementationRef,
    ParameterField,
    ParameterKind,
    ParameterSchema,
    PlacementRequirements,
    ResourceEnvelope,
    SiteClassCapability,
)
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    EvidenceRequirement,
    MissingPolicy,
    Missingness,
    MissingnessStatus,
    OriginClass,
    Requirement,
    RequirementUse,
    SpatialRequirement,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)
from transformations import (
    TransformationPort,
    TransformationSpec,
    TransformationKind,
    ValueSemantics,
)

CONCEPT = "example.scalar.length"
VECTOR_CONCEPT = "example.vector.flow"
SPEED_CONCEPT = "example.scalar.flow_speed"
DIRECTION_CONCEPT = "example.scalar.flow_direction"
SCHEMA_VERSION = "example-scalar-v1"

# The source value and the closed ``m -> km`` registry coefficient fix the
# expected committed result.  Both are asserted by the Stage-4 tests so a
# silently changed conversion cannot pass as success.
SOURCE_METRES = 1500
EXPECTED_KILOMETRES = 1.5

_DIRECT_KM_COST = 9
_METRE_SOURCE_COST = 4
_UNIT_TRANSFORM_COST = 1


@dataclass(frozen=True)
class TransformationFixture:
    """Frozen catalogs and request for the transform-versus-direct contest."""

    base_catalog: CapabilityCatalog
    transformations: tuple[TransformationSpec, ...]
    deployment_snapshot: DeploymentCapabilitySnapshot
    root_uses: tuple[RequirementUse, ...]
    expected_result: float
    expected_cost_units: int
    expected_capability_ids: tuple[str, ...]


def make_unit_bridge_fixture() -> TransformationFixture:
    """Return a graph whose optimum requires an explicit transformation.

    A direct ``km`` producer exists at cost 9.  The cheaper derivation reads a
    ``m`` producer at cost 4 and converts it for cost 1.  Selecting the total-5
    path proves three things at once: ``direct_match`` really did reject the
    metre descriptor for a kilometre requirement, the transformation entered
    the graph as an ordinary costed capability rather than as adapter-internal
    magic, and global selection compared it against real direct data.
    """
    metres = _descriptor(CONCEPT, "m")
    kilometres_derived = _descriptor(
        CONCEPT, "km", origin=OriginClass.DERIVED)
    kilometres_direct = _descriptor(CONCEPT, "km")

    constant_profile = _profile("synthetic.constant.v1")
    transform_profile = _profile("transform.unit_affine.v1")

    metre_source = _constant_spec(
        "example-length-metres", metres, SOURCE_METRES,
        constant_profile, cost=_METRE_SOURCE_COST)
    direct_kilometre_source = _constant_spec(
        "example-length-kilometres-direct", kilometres_direct,
        EXPECTED_KILOMETRES, constant_profile, cost=_DIRECT_KM_COST)

    base_catalog = CapabilityCatalog.freeze(
        (metre_source, direct_kilometre_source), (constant_profile,))

    unit_bridge = TransformationSpec.bind_unit_affine(
        transformation_id="example-metres-to-kilometres",
        transformation_version="1.0.0",
        execution_profile=transform_profile,
        source=metres,
        result=kilometres_derived,
        cost_units=_UNIT_TRANSFORM_COST,
    )

    # The request accepts a derived product.  A requirement restricted to
    # SYNTHETIC origin would legitimately exclude the transformed path, which
    # is the point of keeping origin a first-class consumer constraint.
    root = RequirementUse(
        "stage4-root", "result",
        _requirement(CONCEPT, "km",
                     (OriginClass.DERIVED, OriginClass.SYNTHETIC)))
    return TransformationFixture(
        base_catalog=base_catalog,
        transformations=(unit_bridge,),
        deployment_snapshot=_deployment_snapshot(
            (constant_profile, transform_profile)),
        root_uses=(root,),
        expected_result=EXPECTED_KILOMETRES,
        expected_cost_units=_METRE_SOURCE_COST + _UNIT_TRANSFORM_COST,
        expected_capability_ids=(
            "example-length-metres",
            "transform:example-metres-to-kilometres",
        ),
    )


def make_vector_decomposition_spec() -> TransformationSpec:
    """Return a canonical U/V decomposition edge for model-level coverage.

    Its execution semantics are covered by the closed runtime-operation tests.
    This exists so the *contract* layer is exercised for a multi-output,
    representation-changing transformation whose outputs carry different units
    and distinct concepts.
    """
    source = _descriptor(
        VECTOR_CONCEPT, "m.s-1", component_names=("u", "v"))
    speed = _descriptor(
        SPEED_CONCEPT, "m.s-1", origin=OriginClass.DERIVED,
        component_names=("speed",))
    direction = _descriptor(
        DIRECTION_CONCEPT, "degree", origin=OriginClass.DERIVED,
        component_names=("direction",))
    return TransformationSpec.bind(
        transformation_id="example-flow-uv-to-speed-direction",
        transformation_version="1.0.0",
        kind=TransformationKind.VECTOR_UV_TO_SPEED_DIRECTION,
        execution_profile=_profile(
            "transform.vector_uv_to_speed_direction.v1"),
        input_ports=(TransformationPort(
            "source", source, ValueSemantics.CANONICAL_UV_VECTOR),),
        output_ports=(
            TransformationPort(
                "speed", speed,
                ValueSemantics.SCALAR_CONTINUOUS_INTENSIVE),
            TransformationPort(
                "direction", direction, ValueSemantics.CIRCULAR_DIRECTION),
        ),
        parameters={},
        cost_units=1,
    )


def _constant_spec(
    capability_id: str,
    descriptor: ArtifactDescriptor,
    value: float,
    profile: ExecutionProfile,
    *,
    cost: int,
) -> CapabilitySpec:
    return CapabilitySpec.bind(
        capability_id=capability_id,
        capability_version="1.0.0",
        implementation=profile.implementation,
        binder=BinderRef.from_key("synthetic.constant.bind.v1"),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("value", ParameterKind.NUMBER),)),
        parameterizations=(BindingParameterization(
            {"value": value}, {"cost_units": cost}),),
        execution_profile_id=profile.profile_id,
    )


def _descriptor(
    concept_id: str,
    units: str,
    *,
    origin: OriginClass = OriginClass.SYNTHETIC,
    component_names: tuple[str, ...] = (),
) -> ArtifactDescriptor:
    return ArtifactDescriptor(
        concept_id=concept_id,
        schema_version=SCHEMA_VERSION,
        representation="application/json",
        units=units,
        spatial_support=BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1")),
        temporal_support=TemporalSupport(TemporalKind.TIME_INVARIANT),
        vertical_support=None,
        grid=None,
        native_resolution=None,
        origin=origin,
        missingness=Missingness(MissingnessStatus.COMPLETE),
        component_names=component_names,
    )


def _requirement(
    concept_id: str,
    units: str,
    allowed_origins: tuple[OriginClass, ...],
) -> Requirement:
    return Requirement(
        concept_id=concept_id,
        accepted_schema_versions=(SCHEMA_VERSION,),
        representation=ValueConstraint.exact("application/json"),
        units=ValueConstraint.exact(units),
        spatial=SpatialRequirement(BBoxSupport(
            "EPSG:4326", ("x", "y"), ("-1", "-1", "1", "1"))),
        temporal=TemporalRequirement(TemporalKind.TIME_INVARIANT),
        vertical=None,
        allowed_origins=allowed_origins,
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )


def _profile(operation_key: str) -> ExecutionProfile:
    return ExecutionProfile.bind(
        ImplementationRef.from_operation_key(operation_key),
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1,
                min_memory_mb=64,
                max_cpu_cores=1,
                max_memory_mb=128,
            ),
        ),
    )


def _deployment_snapshot(
    profiles: tuple[ExecutionProfile, ...],
) -> DeploymentCapabilitySnapshot:
    implementation_digests = tuple(sorted({
        value.implementation.implementation_sha256 for value in profiles}))
    return DeploymentCapabilitySnapshot.freeze(
        "2026-08-14T00:00:00Z",
        (SiteClassCapability(
            site_class_id="private-node-example-cpu",
            architecture="x86_64",
            provider_kinds=("stage1-local-subprocess",),
            implementation_digests=implementation_digests,
            environment_classes=(),
            network_classes=("none",),
            credential_classes=(),
            mount_classes=(),
            policy_classes=(),
            max_cpu_cores=1,
            max_memory_mb=128,
            max_gpus=0,
        ),),
    )


__all__ = [
    "CONCEPT",
    "EXPECTED_KILOMETRES",
    "SOURCE_METRES",
    "TransformationFixture",
    "make_unit_bridge_fixture",
    "make_vector_decomposition_spec",
]
