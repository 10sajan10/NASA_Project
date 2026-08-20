"""Typed, snapshot-bound queries over native artifact metadata.

Queries inspect only an immutable :class:`ArtifactRegistrySnapshot`.  They do
not open, copy, convert, reproject, or otherwise reinterpret payload bytes.
Every result identifies the query, the exact snapshot, and the ordered
content/provenance identities that matched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from contracts import (
    GridDescriptor,
    IntrinsicUncertainty,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SpatialScale,
    TemporalKind,
    UncertaintyStatus,
    VerticalKind,
    VerticalSupport,
    canonical_crs,
    canonical_decimal,
    canonical_timestamp,
    canonical_unit,
)
from engine.runtime.identity import strict_hash

from .records import (
    ArtifactAvailability,
    ArtifactInput,
    ArtifactRecord,
    ArtifactRegistrySnapshot,
    ArtifactSnapshotEntry,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RESULT_MINT = object()


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")


def _optional_text(value: str | None, label: str) -> None:
    if value is not None:
        _text(value, label)


def _optional_digest(value: str | None, label: str) -> None:
    if value is not None and (
            not isinstance(value, str) or _DIGEST.fullmatch(value) is None):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _ordered_unique_text(
    values: tuple[str, ...], label: str,
) -> tuple[str, ...]:
    if (not isinstance(values, tuple)
            or any(not isinstance(value, str) or not value.strip()
                   for value in values)):
        raise ValueError(f"{label} must be a tuple of non-empty text")
    ordered = tuple(sorted(set(values)))
    if values != ordered:
        raise ValueError(f"{label} must be unique and sorted")
    return ordered


@dataclass(frozen=True)
class ArtifactSnapshotQuery:
    """Conjunctive typed filters for one registry snapshot.

    Bounds are intersection filters and always require an exact CRS.  Lineage
    filters refer to direct, content-identified input edges; recursive lineage
    traversal is intentionally not inferred by this metadata-only surface.
    """

    record_id: str | None = None
    artifact_id: str | None = None
    content_sha256: str | None = None
    descriptor_id: str | None = None
    concept_id: str | None = None
    representation: str | None = None
    schema_version: str | None = None
    units: str | None = None
    spatial_crs: str | None = None
    intersects_bounds: tuple[str, str, str, str] | None = None
    grid_id: str | None = None
    grid_crs: str | None = None
    grid_shape: tuple[int, int] | None = None
    native_resolution: SpatialScale | None = None
    temporal_kind: TemporalKind | None = None
    intersects_time: tuple[str, str] | None = None
    cadence_s: str | None = None
    vertical_support: VerticalSupport | None = None
    vertical_kind: VerticalKind | None = None
    origin: OriginClass | None = None
    missingness: Missingness | None = None
    missingness_status: MissingnessStatus | None = None
    intrinsic_uncertainty: IntrinsicUncertainty | None = None
    uncertainty_status: UncertaintyStatus | None = None
    required_components: tuple[str, ...] = ()
    evidence_profile_id: str | None = None
    producer_id: str | None = None
    producer_version: str | None = None
    output_port_id: str | None = None
    media_type: str | None = None
    location: str | None = None
    lineage_artifact_ids: tuple[str, ...] = ()
    lineage_port_ids: tuple[str, ...] = ()
    has_lineage: bool | None = None
    availability: ArtifactAvailability | None = ArtifactAvailability.COMMITTED

    def __post_init__(self) -> None:
        for name in (
                "record_id", "artifact_id", "content_sha256",
                "descriptor_id", "grid_id"):
            _optional_digest(getattr(self, name), f"artifact query {name}")
        for name in (
                "concept_id", "representation", "schema_version",
                "evidence_profile_id", "producer_id", "producer_version",
                "output_port_id", "media_type"):
            _optional_text(getattr(self, name), f"artifact query {name}")
        if self.units is not None:
            object.__setattr__(self, "units", canonical_unit(self.units))
        if self.spatial_crs is not None:
            object.__setattr__(
                self, "spatial_crs", canonical_crs(self.spatial_crs))
        if self.grid_crs is not None:
            object.__setattr__(self, "grid_crs", canonical_crs(self.grid_crs))
        if self.intersects_bounds is not None:
            if self.spatial_crs is None:
                raise ValueError(
                    "artifact query bounds require an exact spatial_crs")
            if (not isinstance(self.intersects_bounds, tuple)
                    or len(self.intersects_bounds) != 4):
                raise ValueError(
                    "artifact query bounds require xmin,ymin,xmax,ymax")
            bounds = tuple(canonical_decimal(
                value, "artifact query bound")
                for value in self.intersects_bounds)
            decimal = tuple(Decimal(value) for value in bounds)
            if decimal[0] >= decimal[2] or decimal[1] >= decimal[3]:
                raise ValueError("artifact query bounds are invalid")
            object.__setattr__(self, "intersects_bounds", bounds)
        if self.grid_shape is not None and (
                not isinstance(self.grid_shape, tuple)
                or len(self.grid_shape) != 2
                or any(type(value) is not int or value <= 0
                       for value in self.grid_shape)):
            raise ValueError(
                "artifact query grid_shape needs two positive integers")
        if (self.native_resolution is not None
                and not isinstance(self.native_resolution, SpatialScale)):
            raise TypeError(
                "artifact query native_resolution must be SpatialScale")
        if (self.temporal_kind is not None
                and not isinstance(self.temporal_kind, TemporalKind)):
            raise TypeError("artifact query temporal_kind must be typed")
        if self.intersects_time is not None:
            if (not isinstance(self.intersects_time, tuple)
                    or len(self.intersects_time) != 2):
                raise ValueError(
                    "artifact query time requires an exact start and end")
            interval = tuple(canonical_timestamp(
                value, "artifact query time") for value in self.intersects_time)
            if _timestamp(interval[0]) >= _timestamp(interval[1]):
                raise ValueError("artifact query time interval is invalid")
            object.__setattr__(self, "intersects_time", interval)
        if self.cadence_s is not None:
            cadence = canonical_decimal(self.cadence_s, "artifact query cadence")
            if Decimal(cadence) <= 0:
                raise ValueError("artifact query cadence must be positive")
            object.__setattr__(self, "cadence_s", cadence)
        for value, expected, label in (
            (self.vertical_support, VerticalSupport, "vertical_support"),
            (self.vertical_kind, VerticalKind, "vertical_kind"),
            (self.origin, OriginClass, "origin"),
            (self.missingness, Missingness, "missingness"),
            (self.missingness_status, MissingnessStatus,
             "missingness_status"),
            (self.intrinsic_uncertainty, IntrinsicUncertainty,
             "intrinsic_uncertainty"),
            (self.uncertainty_status, UncertaintyStatus,
             "uncertainty_status"),
        ):
            if value is not None and not isinstance(value, expected):
                raise TypeError(f"artifact query {label} must be typed")
        _ordered_unique_text(
            self.required_components, "artifact query required_components")
        lineage_ids = _ordered_unique_text(
            self.lineage_artifact_ids,
            "artifact query lineage_artifact_ids",
        )
        for value in lineage_ids:
            _optional_digest(value, "artifact query lineage artifact ID")
        _ordered_unique_text(
            self.lineage_port_ids, "artifact query lineage_port_ids")
        if self.has_lineage is not None and type(self.has_lineage) is not bool:
            raise TypeError("artifact query has_lineage must be bool")
        if (self.availability is not None
                and not isinstance(self.availability, ArtifactAvailability)):
            raise TypeError("artifact query availability must be typed")
        if self.location is not None:
            _text(self.location, "artifact query location")
            if not Path(self.location).is_absolute():
                raise ValueError("artifact query location must be absolute")

    @property
    def query_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "stage10d-artifact-snapshot-query-v1",
            "record_id": self.record_id,
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "descriptor_id": self.descriptor_id,
            "concept_id": self.concept_id,
            "representation": self.representation,
            "schema_version": self.schema_version,
            "units": self.units,
            "spatial_crs": self.spatial_crs,
            "intersects_bounds": (
                list(self.intersects_bounds)
                if self.intersects_bounds is not None else None),
            "grid_id": self.grid_id,
            "grid_crs": self.grid_crs,
            "grid_shape": (
                list(self.grid_shape) if self.grid_shape is not None else None),
            "native_resolution": (
                self.native_resolution.to_dict()
                if self.native_resolution is not None else None),
            "temporal_kind": (
                self.temporal_kind.value
                if self.temporal_kind is not None else None),
            "intersects_time": (
                list(self.intersects_time)
                if self.intersects_time is not None else None),
            "cadence_s": self.cadence_s,
            "vertical_support": (
                self.vertical_support.to_dict()
                if self.vertical_support is not None else None),
            "vertical_kind": (
                self.vertical_kind.value
                if self.vertical_kind is not None else None),
            "origin": self.origin.value if self.origin is not None else None,
            "missingness": (
                self.missingness.to_dict()
                if self.missingness is not None else None),
            "missingness_status": (
                self.missingness_status.value
                if self.missingness_status is not None else None),
            "intrinsic_uncertainty": (
                self.intrinsic_uncertainty.to_dict()
                if self.intrinsic_uncertainty is not None else None),
            "uncertainty_status": (
                self.uncertainty_status.value
                if self.uncertainty_status is not None else None),
            "required_components": list(self.required_components),
            "evidence_profile_id": self.evidence_profile_id,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
            "output_port_id": self.output_port_id,
            "media_type": self.media_type,
            "location": self.location,
            "lineage_artifact_ids": list(self.lineage_artifact_ids),
            "lineage_port_ids": list(self.lineage_port_ids),
            "has_lineage": self.has_lineage,
            "availability": (
                self.availability.value
                if self.availability is not None else None),
        }


@dataclass(frozen=True)
class ArtifactSnapshotQueryResult:
    """Ordered content-identified matches bound to one query and snapshot."""

    result_id: str
    query_id: str
    snapshot_id: str
    matches: tuple[ArtifactSnapshotEntry, ...]
    _mint: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._mint is not _RESULT_MINT:
            raise PermissionError(
                "artifact query results must be produced from a snapshot")
        for value, label in (
                (self.result_id, "artifact query result_id"),
                (self.query_id, "artifact query query_id"),
                (self.snapshot_id, "artifact query snapshot_id")):
            _optional_digest(value, label)
        if (not isinstance(self.matches, tuple)
                or not all(isinstance(value, ArtifactSnapshotEntry)
                           for value in self.matches)):
            raise TypeError("artifact query matches must be snapshot entries")
        ordered = tuple(sorted(
            self.matches, key=lambda value: value.record.record_id))
        if self.matches != ordered:
            raise ValueError("artifact query matches must be ordered")
        if len(self.record_ids) != len(set(self.record_ids)):
            raise ValueError("artifact query matches cannot repeat records")
        if self.result_id != self.expected_id():
            raise ValueError("artifact query result identity does not verify")

    @classmethod
    def _bind(
        cls,
        query_id: str,
        snapshot_id: str,
        matches: tuple[ArtifactSnapshotEntry, ...],
    ) -> "ArtifactSnapshotQueryResult":
        ordered = tuple(sorted(
            matches, key=lambda value: value.record.record_id))
        payload = cls._identity_payload(query_id, snapshot_id, ordered)
        return cls(
            result_id=strict_hash(payload),
            query_id=query_id,
            snapshot_id=snapshot_id,
            matches=ordered,
            _mint=_RESULT_MINT,
        )

    @property
    def records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(value.record for value in self.matches)

    @property
    def record_ids(self) -> tuple[str, ...]:
        return tuple(value.record.record_id for value in self.matches)

    @property
    def availability_by_record(
        self,
    ) -> tuple[tuple[str, ArtifactAvailability], ...]:
        return tuple(
            (value.record.record_id, value.availability)
            for value in self.matches)

    @staticmethod
    def _identity_payload(
        query_id: str,
        snapshot_id: str,
        matches: tuple[ArtifactSnapshotEntry, ...],
    ) -> dict[str, object]:
        return {
            "schema": "stage10d-artifact-snapshot-query-result-v1",
            "query_id": query_id,
            "snapshot_id": snapshot_id,
            "matches": [
                {
                    "record_id": value.record.record_id,
                    "artifact_id": value.record.artifact_id,
                    "content_sha256": value.record.content_sha256,
                    "availability": value.availability.value,
                    "reason": value.reason,
                }
                for value in matches
            ],
        }

    def expected_id(self) -> str:
        return strict_hash(self._identity_payload(
            self.query_id, self.snapshot_id, self.matches))

    def to_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            **self._identity_payload(
                self.query_id, self.snapshot_id, self.matches),
        }


class SnapshotArtifactCatalog:
    """Read-only metadata search over exactly one registry snapshot."""

    def __init__(self, snapshot: ArtifactRegistrySnapshot):
        if not isinstance(snapshot, ArtifactRegistrySnapshot):
            raise TypeError(
                "snapshot artifact catalog requires ArtifactRegistrySnapshot")
        self._snapshot = snapshot

    @property
    def snapshot_id(self) -> str:
        return self._snapshot.snapshot_id

    def search(
        self,
        query: ArtifactSnapshotQuery,
    ) -> ArtifactSnapshotQueryResult:
        if not isinstance(query, ArtifactSnapshotQuery):
            raise TypeError("snapshot artifact search requires a typed query")
        matches = tuple(
            entry for entry in self._snapshot.entries
            if _matches(entry, query)
        )
        return ArtifactSnapshotQueryResult._bind(
            query.query_id, self._snapshot.snapshot_id, matches)


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _bounds_intersect(
    offered: tuple[str, str, str, str],
    sought: tuple[str, str, str, str],
) -> bool:
    left = tuple(Decimal(value) for value in offered)
    right = tuple(Decimal(value) for value in sought)
    return not (
        left[2] <= right[0] or left[0] >= right[2]
        or left[3] <= right[1] or left[1] >= right[3]
    )


def _matches(
    entry: ArtifactSnapshotEntry,
    query: ArtifactSnapshotQuery,
) -> bool:
    record = entry.record
    descriptor = record.descriptor
    grid: GridDescriptor | None = descriptor.grid
    temporal = descriptor.temporal_support
    vertical = descriptor.vertical_support
    inputs: tuple[ArtifactInput, ...] = record.inputs
    exact = (
        (query.record_id, record.record_id),
        (query.artifact_id, record.artifact_id),
        (query.content_sha256, record.content_sha256),
        (query.descriptor_id, descriptor.descriptor_id),
        (query.concept_id, descriptor.concept_id),
        (query.representation, descriptor.representation),
        (query.schema_version, descriptor.schema_version),
        (query.units, descriptor.units),
        (query.spatial_crs, descriptor.spatial_support.crs),
        (query.grid_crs, grid.crs if grid is not None else None),
        (query.grid_shape, grid.shape if grid is not None else None),
        (query.native_resolution, descriptor.native_resolution),
        (query.temporal_kind, temporal.kind),
        (query.cadence_s, temporal.cadence_s),
        (query.vertical_support, vertical),
        (query.vertical_kind, vertical.kind if vertical is not None else None),
        (query.origin, descriptor.origin),
        (query.missingness, descriptor.missingness),
        (query.missingness_status, descriptor.missingness.status),
        (query.intrinsic_uncertainty, descriptor.intrinsic_uncertainty),
        (query.uncertainty_status,
         descriptor.intrinsic_uncertainty.status),
        (query.evidence_profile_id, record.evidence_profile_id),
        (query.producer_id, record.producer_id),
        (query.producer_version, record.producer_version),
        (query.output_port_id, record.output_port_id),
        (query.media_type, record.media_type),
        (query.location, record.location),
        (query.availability, entry.availability),
    )
    if any(sought is not None and sought != offered
           for sought, offered in exact):
        return False
    if query.grid_id is not None and (
            grid is None or grid.grid_id != query.grid_id):
        return False
    if query.intersects_bounds is not None and not _bounds_intersect(
            descriptor.spatial_support.bounds, query.intersects_bounds):
        return False
    if query.intersects_time is not None:
        if temporal.start is None or temporal.end is None:
            return False
        sought_start, sought_end = query.intersects_time
        if (_timestamp(temporal.end) <= _timestamp(sought_start)
                or _timestamp(temporal.start) >= _timestamp(sought_end)):
            return False
    if not set(query.required_components).issubset(
            descriptor.component_names):
        return False
    input_artifact_ids = {value.artifact_id for value in inputs}
    if not set(query.lineage_artifact_ids).issubset(input_artifact_ids):
        return False
    input_port_ids = {value.port_id for value in inputs}
    if not set(query.lineage_port_ids).issubset(input_port_ids):
        return False
    if query.has_lineage is not None and bool(inputs) is not query.has_lineage:
        return False
    return True


__all__ = [
    "ArtifactSnapshotQuery",
    "ArtifactSnapshotQueryResult",
    "SnapshotArtifactCatalog",
]
