"""Domain-neutral Stage-5 acquisition fixtures.

The concepts here are meaningless field labels.  Stage 5 has to show that an
acquired remote artifact competes on cost with a pinned local one, inside the
same global selector, without the resolver knowing what any of it means.

The shape is deliberately wind-*shaped* — a two-dimensional field with units, a
CRS, a time window, and tiles that only jointly cover the request — because
that shape is what exercises coverage, containment, unit conversion, and
source-versus-model choice.  No code below is specific to wind, and no resolver
code knows this fixture exists.

Both connectors run in this process.  They implement the real
:class:`~acquisition.connector.SourceConnector` contract, including pagination,
conditional identity, mutation, and outages, but they contact no network, so
the whole suite stays hermetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from grid_convention import FIELD_JSON_SCHEMA

from acquisition import (
    AssetCandidate,
    AssetExtent,
    AssetMissingError,
    AssetMutatedError,
    AcquisitionRequest,
    AssemblyMode,
    ConditionalIdentityKind,
    AssetConditionalIdentity,
    CredentialRef,
    FetchAuthorization,
    MetadataPage,
    MetadataQuery,
    SecondOrderQuerySpec,
    SourceConnector,
    SourceDescriptor,
    SourceSchema,
    TransientSourceError,
)
from capabilities import (
    BinderRef,
    BindingParameterization,
    CapabilitySpec,
    DeploymentCapabilitySnapshot,
    DescriptorTemplate,
    ExecutionProfile,
    ImplementationRef,
    InputPortTemplate,
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
    GridDescriptor,
    Requirement,
    RequirementUse,
    SampleSemantics,
    ScaleBasis,
    SpatialRequirement,
    SpatialScale,
    TemporalKind,
    TemporalRequirement,
    TemporalSupport,
    ValueConstraint,
)
from engine.runtime.identity import strict_canonical_json
from transformations import (
    TransformationSpec,
)

SCHEMA_VERSION = FIELD_JSON_SCHEMA
REPRESENTATION = "application/json"
FLOW_CONCEPT = "example.field.flow_speed"
SUPPORT_CONCEPT = "example.field.support"

COARSE_UNITS = "m.s-1"
TARGET_UNITS = "km.h-1"
SUPPORT_UNITS = "m"

CRS = "EPSG:4326"
AXES = ("x", "y")

LOCAL_SOURCE_ID = "local-pinned-store"
REMOTE_SOURCE_ID = "remote-tiled-archive"

TARGET_BOUNDS = ("0", "0", "4", "4")
WINDOW_START = "2026-01-01T00:00:00Z"
WINDOW_END = "2026-01-01T02:00:00Z"
CADENCE_S = "3600"
MAX_GAP_S = "0"

# Costs chosen so the *acquired-plus-converted* path must beat the pinned local
# artifact.  If the resolver ever prefers 8 over 5 the contest has stopped
# being a contest and the test should fail loudly.
LOCAL_COST = 8
REMOTE_COST = 3
UNIT_TRANSFORM_COST = 2
SUPPORT_COST = 1
MODEL_COST = 4

WEST_VALUE = 10.0
EAST_VALUE = 20.0
MPS_TO_KMPH = 3.6

TIME_STEPS = (
    "2026-01-01T00:00:00Z",
    "2026-01-01T01:00:00Z",
)
Y_COORDS = (0.0, 2.0, 4.0)
WEST_X = (-1.0, 0.0, 1.0)
EAST_X = (2.0, 3.0, 4.0)
FAR_X = (6.0, 7.0, 8.0)


def field_grid() -> GridDescriptor:
    """The exact Stage-5 payload lattice, in sample-centre convention."""
    return GridDescriptor(
        CRS, AXES, (len(Y_COORDS), len(WEST_X + EAST_X)),
        ("1", "0", "-1", "0", "2", "0"),
        SpatialScale("1", "2", "degree", ScaleBasis.ANGULAR),
    )


def target_bbox() -> BBoxSupport:
    return BBoxSupport(CRS, AXES, TARGET_BOUNDS)


def target_window() -> TemporalSupport:
    return TemporalSupport(
        TemporalKind.SERIES, start=WINDOW_START, end=WINDOW_END,
        cadence_s=CADENCE_S, max_gap_s=MAX_GAP_S,
        sample_semantics=SampleSemantics.INSTANTANEOUS)


def _field(x: tuple[float, ...], value: float) -> dict[str, Any]:
    return {
        "schema": "field-json-v2",
        "crs": CRS,
        "axis_order": ["x", "y"],
        "x": list(x),
        "y": list(Y_COORDS),
        "time": list(TIME_STEPS),
        "components": {
            "speed": [[[value for _ in x] for _ in Y_COORDS]
                      for _ in TIME_STEPS],
        },
    }


def _payload(value: dict[str, Any]) -> bytes:
    return strict_canonical_json(value).encode("utf-8")


def expected_converted_field() -> dict[str, Any]:
    """The field the demo must commit: both tiles joined, then converted."""
    return {
        "schema": "field-json-v2",
        "crs": CRS,
        "axis_order": ["x", "y"],
        "x": list(WEST_X + EAST_X),
        "y": list(Y_COORDS),
        "time": list(TIME_STEPS),
        "components": {
            "speed": [[[WEST_VALUE * MPS_TO_KMPH] * len(WEST_X)
                       + [EAST_VALUE * MPS_TO_KMPH] * len(EAST_X)
                       for _ in Y_COORDS] for _ in TIME_STEPS],
        },
    }


@dataclass
class _Entry:
    candidate: AssetCandidate
    payload: bytes
    concept_id: str
    present: bool = True


class FixtureConnector(SourceConnector):
    """An in-process connector with real pagination and conditional identity.

    It supports the failure modes Stage 5 has to distinguish: a transient
    outage that must be retried against the same binding, an asset that
    mutates after binding, and an asset that disappears entirely.
    """

    def __init__(self, descriptor: SourceDescriptor,
                 entries: Iterable[_Entry]) -> None:
        super().__init__(descriptor)
        self._entries = {item.candidate.asset_id: item for item in entries}
        self.transient_searches = 0
        self.transient_payloads = 0
        self.payload_calls = 0

    # -- test controls ----------------------------------------------------

    def mutate(self, asset_id: str, new_value: str) -> None:
        entry = self._entries[asset_id]
        identity = entry.candidate.conditional_identity
        assert identity is not None
        entry.candidate = AssetCandidate(
            asset_id=entry.candidate.asset_id,
            locator=entry.candidate.locator,
            extent=entry.candidate.extent,
            byte_size=entry.candidate.byte_size,
            conditional_identity=AssetConditionalIdentity(
                identity.kind, new_value),
        )

    def remove(self, asset_id: str) -> None:
        self._entries[asset_id].present = False

    def replace_payload_without_version(self, asset_id: str,
                                        payload: bytes) -> None:
        """Adversarial provider control: violate an unchanged weak ETag.

        The replacement must keep its declared size so metadata and manifest
        identity remain byte-for-byte identical.  Stage-8R uses this to prove
        post-fetch content, rather than the weak ETag, reaches task identity.
        """
        entry = self._entries[asset_id]
        if len(payload) != entry.candidate.byte_size:
            raise ValueError("adversarial replacement must preserve byte_size")
        entry.payload = payload

    def observed_identities(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for asset_id, entry in self._entries.items():
            if not entry.present or entry.candidate.conditional_identity is None:
                result[asset_id] = None
            else:
                result[asset_id] = entry.candidate.conditional_identity.value
        return result

    # -- connector contract ----------------------------------------------

    def search_metadata(self, query: MetadataQuery, cursor: str | None,
                        limit: int) -> MetadataPage:
        self.search_calls += 1
        if self.transient_searches > 0:
            self.transient_searches -= 1
            raise TransientSourceError(
                f"{self.source_id} metadata endpoint is briefly unavailable")
        matches = [
            entry for entry in self._entries.values()
            if entry.present
            and entry.concept_id == query.concept_id
            and _intersects(entry.candidate.extent, query)
        ]
        matches.sort(key=lambda item: item.candidate.asset_id)
        start = 0 if cursor is None else int(cursor)
        window = matches[start:start + limit]
        next_index = start + len(window)
        next_cursor = str(next_index) if next_index < len(matches) else None
        return MetadataPage(
            tuple(item.candidate for item in window), next_cursor)

    def open_payload(self, authorization: FetchAuthorization, asset_id: str,
                     locator: str,
                     expected: AssetConditionalIdentity) -> bytes:
        if not isinstance(authorization, FetchAuthorization):
            raise PermissionError("payload requires a minted authorization")
        if not authorization.authorizes(self.source_id, asset_id):
            raise PermissionError(
                f"asset {asset_id!r} is not covered by the bound manifest")
        self.payload_calls += 1
        if self.transient_payloads > 0:
            self.transient_payloads -= 1
            raise TransientSourceError(
                f"{self.source_id} transfer endpoint is briefly unavailable")
        entry = self._entries.get(asset_id)
        if entry is None or not entry.present:
            raise AssetMissingError(asset_id)
        current = entry.candidate.conditional_identity
        if current is None or current.value != expected.value:
            raise AssetMutatedError(
                asset_id, expected.value,
                "" if current is None else current.value)
        self.bytes_transferred += len(entry.payload)
        return entry.payload


def _intersects(extent: AssetExtent, query: MetadataQuery) -> bool:
    from contracts.identity import decimal_value, timestamp_value
    box = extent.spatial
    if box.crs != query.spatial.crs or box.axis_order != query.spatial.axis_order:
        return False
    a = tuple(decimal_value(item) for item in box.bounds)
    b = tuple(decimal_value(item) for item in query.spatial.bounds)
    if a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]:
        return False
    left, right = extent.temporal, query.temporal
    if (left.kind is TemporalKind.TIME_INVARIANT
            or right.kind is TemporalKind.TIME_INVARIANT):
        return left.kind is right.kind
    return not (timestamp_value(left.end) <= timestamp_value(right.start)
                or timestamp_value(right.end) <= timestamp_value(left.start))


def _extent(bounds: tuple[str, str, str, str]) -> AssetExtent:
    return AssetExtent(
        BBoxSupport(CRS, AXES, bounds),
        TemporalSupport(TemporalKind.SERIES, start=WINDOW_START,
                        end=WINDOW_END, cadence_s=CADENCE_S,
                        max_gap_s=MAX_GAP_S,
                        sample_semantics=SampleSemantics.INSTANTANEOUS))


def _checksum_candidate(asset_id: str, bounds: tuple[str, str, str, str],
                        payload: bytes) -> AssetCandidate:
    import hashlib
    return AssetCandidate(
        asset_id=asset_id,
        locator=f"file:///pinned/{asset_id}.json",
        extent=_extent(bounds),
        byte_size=len(payload),
        conditional_identity=AssetConditionalIdentity(
            ConditionalIdentityKind.CHECKSUM_SHA256,
            hashlib.sha256(payload).hexdigest()),
    )


def _etag_candidate(asset_id: str, bounds: tuple[str, str, str, str],
                    payload: bytes, etag: str) -> AssetCandidate:
    return AssetCandidate(
        asset_id=asset_id,
        locator=f"https://archive.invalid/{asset_id}",
        extent=_extent(bounds),
        byte_size=len(payload),
        conditional_identity=AssetConditionalIdentity(
            ConditionalIdentityKind.ETAG, etag),
    )


def make_local_connector() -> FixtureConnector:
    """One pinned, content-addressed artifact already in target units."""
    payload = _payload(_field(WEST_X + EAST_X, 50.0))
    return FixtureConnector(
        SourceDescriptor(
            source_id=LOCAL_SOURCE_ID,
            provider_kind="LOCAL_FILE",
            network_class="none",
            credential_ref=CredentialRef("none", "unauthenticated"),
            supports_conditional_fetch=True,
            page_size=10,
            schemas=(SourceSchema(
                source_id=LOCAL_SOURCE_ID,
                concept_id=FLOW_CONCEPT,
                schema_version=SCHEMA_VERSION,
                representation=REPRESENTATION,
                units=TARGET_UNITS,
                spatial_crs=CRS,
                spatial_axis_order=AXES,
                temporal_kind=TemporalKind.SERIES,
                sample_semantics=SampleSemantics.INSTANTANEOUS,
                origin=OriginClass.OBSERVATION,
                assembly_mode=AssemblyMode.SINGLE_ASSET,
                component_names=("speed",),
                grid=field_grid(),
            ),),
        ),
        (_Entry(_checksum_candidate("local-fine-0", field_grid().support_bounds,
                                    payload), payload, FLOW_CONCEPT),),
    )


def make_remote_connector(*, page_size: int = 1) -> FixtureConnector:
    """A paginated archive of coarse tiles that overhang the request.

    The tiles are in ``m.s-1``, so this source cannot satisfy a ``km.h-1``
    requirement by itself; it needs the Stage-4 conversion, which is the point.
    Their union is larger than the requested region, which is what makes the
    second-order support query genuinely unknowable in round zero.
    """
    west = _payload(_field(WEST_X, WEST_VALUE))
    east = _payload(_field(EAST_X, EAST_VALUE))
    far = _payload(_field(FAR_X, 99.0))
    support = _payload(_field(WEST_X + EAST_X, 1.0))
    entries = (
        _Entry(_etag_candidate("remote-tile-west", ("-1.5", "-1", "1.5", "5"),
                               west, "etag-west-1"), west, FLOW_CONCEPT),
        _Entry(_etag_candidate("remote-tile-east", ("1.5", "-1", "4.5", "5"),
                               east, "etag-east-1"), east, FLOW_CONCEPT),
        # Outside the request: proves the connector filters rather than the
        # binder silently dropping unrelated assets later.
        _Entry(_etag_candidate("remote-tile-far", ("5.5", "-1", "8.5", "5"),
                               far, "etag-far-1"), far, FLOW_CONCEPT),
        # Only discoverable once the tile union above is known.
        _Entry(_etag_candidate("remote-support-0", field_grid().support_bounds,
                               support, "etag-support-1"), support,
               SUPPORT_CONCEPT),
    )
    return FixtureConnector(
        SourceDescriptor(
            source_id=REMOTE_SOURCE_ID,
            provider_kind="REMOTE_HTTP",
            network_class="public-internet",
            credential_ref=CredentialRef("env", "STAGE5_ARCHIVE_TOKEN"),
            supports_conditional_fetch=True,
            page_size=page_size,
            schemas=(
                SourceSchema(
                    source_id=REMOTE_SOURCE_ID,
                    concept_id=FLOW_CONCEPT,
                    schema_version=SCHEMA_VERSION,
                    representation=REPRESENTATION,
                    units=COARSE_UNITS,
                    spatial_crs=CRS,
                    spatial_axis_order=AXES,
                    temporal_kind=TemporalKind.SERIES,
                    sample_semantics=SampleSemantics.INSTANTANEOUS,
                    origin=OriginClass.OBSERVATION,
                    assembly_mode=AssemblyMode.FIELD_JSON_X_TILES_V1,
                    component_names=("speed",),
                    grid=field_grid(),
                ),
                SourceSchema(
                    source_id=REMOTE_SOURCE_ID,
                    concept_id=SUPPORT_CONCEPT,
                    schema_version=SCHEMA_VERSION,
                    representation=REPRESENTATION,
                    units=SUPPORT_UNITS,
                    spatial_crs=CRS,
                    spatial_axis_order=AXES,
                    temporal_kind=TemporalKind.SERIES,
                    sample_semantics=SampleSemantics.INSTANTANEOUS,
                    origin=OriginClass.OBSERVATION,
                    assembly_mode=AssemblyMode.SINGLE_ASSET,
                    component_names=("speed",),
                    grid=field_grid(),
                ),
            ),
        ),
        entries,
    )


def local_query() -> MetadataQuery:
    return MetadataQuery(
        source_id=LOCAL_SOURCE_ID, concept_id=FLOW_CONCEPT,
        schema_version=SCHEMA_VERSION, units=TARGET_UNITS,
        representation=REPRESENTATION, spatial=target_bbox(),
        temporal=target_window())


def remote_query() -> MetadataQuery:
    return MetadataQuery(
        source_id=REMOTE_SOURCE_ID, concept_id=FLOW_CONCEPT,
        schema_version=SCHEMA_VERSION, units=COARSE_UNITS,
        representation=REPRESENTATION, spatial=target_bbox(),
        temporal=target_window())


def support_rule() -> SecondOrderQuerySpec:
    """The downscaling model's follow-up question about its own inputs."""
    return SecondOrderQuerySpec(
        rule_id="second_order.support_over_discovered_extent.v1",
        trigger_query_id=remote_query().query_id,
        source_id=REMOTE_SOURCE_ID,
        concept_id=SUPPORT_CONCEPT,
        schema_version=SCHEMA_VERSION,
        units=SUPPORT_UNITS,
        representation=REPRESENTATION,
    )


def acquisition_requests() -> tuple[AcquisitionRequest, ...]:
    return (
        AcquisitionRequest(query=local_query(), target_spatial=target_bbox(),
                           target_temporal=target_window()),
        AcquisitionRequest(query=remote_query(), target_spatial=target_bbox(),
                           target_temporal=target_window()),
    )


# -- descriptors, requirement, and the profiles the catalog needs ---------


def descriptor(concept_id: str, units: str, bounds: tuple[str, str, str, str],
               origin: OriginClass) -> ArtifactDescriptor:
    grid = field_grid()
    support = BBoxSupport(CRS, AXES, bounds)
    grid.require_support(support)
    return ArtifactDescriptor(
        concept_id=concept_id,
        schema_version=SCHEMA_VERSION,
        representation=REPRESENTATION,
        units=units,
        spatial_support=support,
        temporal_support=TemporalSupport(
            TemporalKind.SERIES, start=WINDOW_START, end=WINDOW_END,
            cadence_s=CADENCE_S, max_gap_s=MAX_GAP_S,
            sample_semantics=SampleSemantics.INSTANTANEOUS),
        vertical_support=None,
        grid=grid,
        native_resolution=None,
        origin=origin,
        missingness=Missingness(MissingnessStatus.COMPLETE),
        component_names=("speed",),
    )


def root_requirement() -> Requirement:
    return Requirement(
        concept_id=FLOW_CONCEPT,
        accepted_schema_versions=(SCHEMA_VERSION,),
        representation=ValueConstraint.exact(REPRESENTATION),
        units=ValueConstraint.exact(TARGET_UNITS),
        spatial=SpatialRequirement(target_bbox()),
        temporal=TemporalRequirement(
            TemporalKind.SERIES, start=WINDOW_START, end=WINDOW_END,
            cadence_s=CADENCE_S),
        vertical=None,
        allowed_origins=(OriginClass.OBSERVATION, OriginClass.DERIVED),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )


def root_uses() -> tuple[RequirementUse, ...]:
    return (RequirementUse("stage5-root", "result", root_requirement()),)


def acquisition_profile() -> ExecutionProfile:
    return _profile("acquisition.materialize.v1")


def transform_profile() -> ExecutionProfile:
    return _profile("transform.unit_affine.v1")


def model_profile() -> ExecutionProfile:
    return _profile("synthetic.add.v1")


def _profile(operation_key: str) -> ExecutionProfile:
    return ExecutionProfile.bind(
        ImplementationRef.from_operation_key(operation_key),
        PlacementRequirements(
            architectures=("x86_64",),
            provider_kinds=("stage1-local-subprocess",),
            resources=ResourceEnvelope(
                min_cpu_cores=1, min_memory_mb=64,
                max_cpu_cores=1, max_memory_mb=256,
            ),
        ),
    )


def deployment_snapshot() -> DeploymentCapabilitySnapshot:
    profiles = (acquisition_profile(), transform_profile(), model_profile())
    digests = tuple(sorted({
        value.implementation.implementation_sha256 for value in profiles}))
    return DeploymentCapabilitySnapshot.freeze(
        "2026-08-15T00:00:00Z",
        (SiteClassCapability(
            site_class_id="private-node-example-cpu",
            architecture="x86_64",
            provider_kinds=("stage1-local-subprocess",),
            implementation_digests=digests,
            environment_classes=(),
            network_classes=("none",),
            credential_classes=(),
            mount_classes=(),
            policy_classes=(),
            max_cpu_cores=1,
            max_memory_mb=256,
            max_gpus=0,
        ),),
    )


def unit_transform(source: ArtifactDescriptor,
                   result: ArtifactDescriptor) -> TransformationSpec:
    """The Stage-4 bridge from the coarse source units to the request units."""
    return TransformationSpec.bind_unit_affine(
        transformation_id="example-mps-to-kmph",
        transformation_version="1.0.0",
        execution_profile=transform_profile(),
        source=source,
        result=result,
        cost_units=UNIT_TRANSFORM_COST,
    )


def downscale_model(coarse: ArtifactDescriptor, support: ArtifactDescriptor,
                    result: ArtifactDescriptor) -> CapabilitySpec:
    """A meaningless two-input producer standing in for a downscaling model.

    Its only job in this fixture is to be the reason a *second-order* data
    query exists: it consumes support data over whatever extent the coarse
    source actually returned, which nothing can know before round one.  It is
    priced so that it loses, so the demo also shows an admissible alternative
    being rejected on cost rather than on feasibility.
    """
    return CapabilitySpec.bind(
        capability_id="example-downscale-model",
        capability_version="1.0.0",
        implementation=model_profile().implementation,
        binder=BinderRef.from_key("synthetic.add.bind.v1"),
        input_ports=(
            InputPortTemplate("left", _exact_requirement(coarse)),
            InputPortTemplate("right", _exact_requirement(support)),
        ),
        output_ports=(DescriptorTemplate("result", result),),
        parameter_schema=ParameterSchema(()),
        parameterizations=(BindingParameterization(
            {}, {"cost_units": MODEL_COST}),),
        execution_profile_id=model_profile().profile_id,
    )


def _exact_requirement(value: ArtifactDescriptor) -> Requirement:
    return Requirement(
        concept_id=value.concept_id,
        accepted_schema_versions=(value.schema_version,),
        representation=ValueConstraint.exact(value.representation),
        units=ValueConstraint.exact(value.units),
        spatial=SpatialRequirement(value.spatial_support),
        temporal=TemporalRequirement(
            TemporalKind.SERIES,
            start=value.temporal_support.start,
            end=value.temporal_support.end,
            cadence_s=CADENCE_S),
        vertical=None,
        allowed_origins=(value.origin,),
        max_native_resolution=None,
        max_effective_resolution=None,
        missing_policy=MissingPolicy(),
        minimum_evidence=EvidenceRequirement(allow_unknown_empirical=True),
    )


__all__ = [
    "COARSE_UNITS",
    "FLOW_CONCEPT",
    "FixtureConnector",
    "LOCAL_COST",
    "LOCAL_SOURCE_ID",
    "MODEL_COST",
    "REMOTE_COST",
    "REMOTE_SOURCE_ID",
    "SCHEMA_VERSION",
    "SUPPORT_CONCEPT",
    "SUPPORT_UNITS",
    "TARGET_UNITS",
    "UNIT_TRANSFORM_COST",
    "acquisition_profile",
    "acquisition_requests",
    "deployment_snapshot",
    "descriptor",
    "downscale_model",
    "expected_converted_field",
    "local_query",
    "make_local_connector",
    "make_remote_connector",
    "model_profile",
    "remote_query",
    "root_requirement",
    "root_uses",
    "support_rule",
    "target_bbox",
    "target_window",
    "transform_profile",
    "unit_transform",
]
