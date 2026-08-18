"""Frozen source semantics and the only admissible descriptor derivation.

Provider metadata may tell discovery *where* an asset is and which immutable
version token addresses it.  It is not allowed to invent scientific schema
facts. Those facts live in a source-controlled :class:`SourceSchema`. Temporal
support comes from the coverage target the binder proved; a gridded payload's
spatial support comes from the schema's exact sample lattice after that lattice
is shown to contain the target.

The assembly mode is part of the schema because coverage and executable
materialisation are different claims.  The Stage-8R MVP admits only payload
layouts the local runtime can assemble without interpolation or inference.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from capabilities.implementation import _required_text
from contracts import (
    ArtifactDescriptor,
    BBoxSupport,
    GridDescriptor,
    IntrinsicUncertainty,
    Missingness,
    MissingnessStatus,
    OriginClass,
    SampleSemantics,
    SpatialScale,
    TemporalKind,
    TemporalSupport,
    VerticalSupport,
)
from engine.runtime.identity import require_object_fields, strict_hash
from grid_convention import FIELD_JSON_SCHEMA


class AssemblyMode(str, Enum):
    """Closed payload layouts implemented by ``acquisition.materialize``."""

    SINGLE_ASSET = "SINGLE_ASSET"
    FIELD_JSON_X_TILES_V1 = "FIELD_JSON_X_TILES_V1"


@dataclass(frozen=True)
class SourceSchema:
    """Immutable scientific facts a connector is authorized to publish.

    Temporal windows are intentionally absent. They are not static schema facts
    and must be derived from a coverage target proven against an exact asset
    manifest. A grid, when present, is a static payload-layout fact and names
    the exact materialized footprint rather than the requested subset.
    """

    source_id: str
    concept_id: str
    schema_version: str
    representation: str
    units: str
    spatial_crs: str
    spatial_axis_order: tuple[str, str]
    temporal_kind: TemporalKind
    sample_semantics: SampleSemantics
    origin: OriginClass
    assembly_mode: AssemblyMode
    component_names: tuple[str, ...] = ()
    vertical_support: VerticalSupport | None = None
    grid: GridDescriptor | None = None
    native_resolution: SpatialScale | None = None
    missingness: Missingness = field(
        default_factory=lambda: Missingness(MissingnessStatus.COMPLETE))
    intrinsic_uncertainty: IntrinsicUncertainty = field(
        default_factory=lambda: IntrinsicUncertainty.unknown(
            "NOT_REPORTED_BY_SOURCE"))

    def __post_init__(self) -> None:
        for value, label in (
                (self.source_id, "source schema source_id"),
                (self.concept_id, "source schema concept_id"),
                (self.schema_version, "source schema schema_version"),
                (self.representation, "source schema representation"),
                (self.units, "source schema units"),
                (self.spatial_crs, "source schema CRS")):
            _required_text(value, label)
        if (not isinstance(self.spatial_axis_order, tuple)
                or len(self.spatial_axis_order) != 2
                or self.spatial_axis_order[0] == self.spatial_axis_order[1]
                or any(not isinstance(item, str) or not item
                       for item in self.spatial_axis_order)):
            raise ValueError("source schema needs two distinct ordered axes")
        for value, expected, label in (
                (self.temporal_kind, TemporalKind, "temporal kind"),
                (self.sample_semantics, SampleSemantics, "sample semantics"),
                (self.origin, OriginClass, "origin"),
                (self.assembly_mode, AssemblyMode, "assembly mode"),
                (self.missingness, Missingness, "missingness"),
                (self.intrinsic_uncertainty, IntrinsicUncertainty,
                 "intrinsic uncertainty")):
            if not isinstance(value, expected):
                raise TypeError(f"source schema {label} must be typed")
        if self.vertical_support is not None and not isinstance(
                self.vertical_support, VerticalSupport):
            raise TypeError("source schema vertical support must be typed")
        if self.grid is not None and not isinstance(self.grid, GridDescriptor):
            raise TypeError("source schema grid must be typed")
        if self.native_resolution is not None and not isinstance(
                self.native_resolution, SpatialScale):
            raise TypeError("source schema native resolution must be typed")
        if (not isinstance(self.component_names, tuple)
                or any(not isinstance(item, str) or not item.strip()
                       for item in self.component_names)
                or self.component_names
                != tuple(sorted(set(self.component_names)))):
            raise ValueError(
                "source schema component names must be unique sorted text")
        if self.grid is not None and (
                self.grid.crs != self.spatial_crs
                or self.grid.axis_order != self.spatial_axis_order):
            raise ValueError("source grid CRS/axes disagree with source schema")
        if self.assembly_mode is AssemblyMode.FIELD_JSON_X_TILES_V1:
            if self.schema_version != FIELD_JSON_SCHEMA:
                raise ValueError(
                    "FIELD_JSON_X_TILES_V1 emits field-json-v2 and cannot "
                    "authorize another schema version")
            if self.representation != "application/json":
                raise ValueError(
                    "FIELD_JSON_X_TILES_V1 requires application/json")
            if self.grid is None:
                raise ValueError(
                    "FIELD_JSON_X_TILES_V1 requires an exact source grid")
        if self.schema_version == FIELD_JSON_SCHEMA:
            if self.grid is None:
                raise ValueError(
                    "field-json-v2 source schema requires an exact grid")
            if not self.component_names:
                raise ValueError(
                    "field-json-v2 source schema requires component names")
            self.grid.require_canonical_affine()

    @property
    def schema_id(self) -> str:
        return strict_hash(self.to_dict())

    def validates_query(self, query: Any) -> None:
        """Refuse a query that relabels this frozen source schema."""
        for name, expected in (
                ("source_id", self.source_id),
                ("concept_id", self.concept_id),
                ("schema_version", self.schema_version),
                ("representation", self.representation),
                ("units", self.units)):
            if getattr(query, name, None) != expected:
                raise ValueError(
                    f"query {name} is not authorized by source schema "
                    f"{self.schema_id}")
        spatial = getattr(query, "spatial", None)
        temporal = getattr(query, "temporal", None)
        if (not isinstance(spatial, BBoxSupport)
                or spatial.crs != self.spatial_crs
                or spatial.axis_order != self.spatial_axis_order):
            raise ValueError("query spatial CRS/axes are not in source schema")
        if (not isinstance(temporal, TemporalSupport)
                or temporal.kind is not self.temporal_kind
                or temporal.sample_semantics is not self.sample_semantics):
            raise ValueError(
                "query temporal kind/semantics are not in source schema")

    def derive_descriptor(
        self,
        spatial: BBoxSupport,
        temporal: TemporalSupport,
    ) -> ArtifactDescriptor:
        """Derive an output descriptor from proven support and frozen grid."""
        if not isinstance(spatial, BBoxSupport):
            raise TypeError("derived descriptor spatial support must be typed")
        if not isinstance(temporal, TemporalSupport):
            raise TypeError("derived descriptor temporal support must be typed")
        if (spatial.crs != self.spatial_crs
                or spatial.axis_order != self.spatial_axis_order):
            raise ValueError("proven coverage is outside source schema CRS/axes")
        if (temporal.kind is not self.temporal_kind
                or temporal.sample_semantics is not self.sample_semantics):
            raise ValueError(
                "proven temporal support is outside source schema semantics")
        derived_spatial = spatial
        if self.grid is not None:
            # The coverage target says what the request needed.  A materialized
            # gridded payload may include deterministic overhang (for example,
            # complete edge tiles), so its descriptor must name the exact grid
            # footprint rather than relabeling the bytes as the request box.
            grid_spatial = BBoxSupport(
                self.grid.crs, self.grid.axis_order,
                self.grid.support_bounds)
            if not grid_spatial.contains(spatial):
                raise ValueError(
                    "source grid does not contain the proven coverage target")
            derived_spatial = grid_spatial
        return ArtifactDescriptor(
            concept_id=self.concept_id,
            schema_version=self.schema_version,
            representation=self.representation,
            units=self.units,
            spatial_support=derived_spatial,
            temporal_support=temporal,
            vertical_support=self.vertical_support,
            grid=self.grid,
            native_resolution=self.native_resolution,
            origin=self.origin,
            missingness=self.missingness,
            intrinsic_uncertainty=self.intrinsic_uncertainty,
            component_names=self.component_names,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage8r-source-schema-v1",
            "source_id": self.source_id,
            "concept_id": self.concept_id,
            "schema_version": self.schema_version,
            "representation": self.representation,
            "units": self.units,
            "spatial_crs": self.spatial_crs,
            "spatial_axis_order": list(self.spatial_axis_order),
            "temporal_kind": self.temporal_kind.value,
            "sample_semantics": self.sample_semantics.value,
            "origin": self.origin.value,
            "assembly_mode": self.assembly_mode.value,
            "component_names": list(self.component_names),
            "vertical_support": (self.vertical_support.to_dict()
                                 if self.vertical_support is not None else None),
            "grid": self.grid.to_dict() if self.grid is not None else None,
            "native_resolution": (self.native_resolution.to_dict()
                                  if self.native_resolution is not None else None),
            "missingness": self.missingness.to_dict(),
            "intrinsic_uncertainty": self.intrinsic_uncertainty.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceSchema":
        raw = require_object_fields(
            value,
            {"schema", "source_id", "concept_id", "schema_version",
             "representation", "units", "spatial_crs",
             "spatial_axis_order", "temporal_kind", "sample_semantics",
            "origin", "assembly_mode", "component_names",
            "vertical_support", "grid",
             "native_resolution", "missingness", "intrinsic_uncertainty"},
            "SourceSchema")
        if raw.pop("schema") != "stage8r-source-schema-v1":
            raise ValueError("SourceSchema schema is not stage8r-source-schema-v1")
        raw["spatial_axis_order"] = tuple(raw["spatial_axis_order"])
        raw["temporal_kind"] = TemporalKind(raw["temporal_kind"])
        raw["sample_semantics"] = SampleSemantics(raw["sample_semantics"])
        raw["origin"] = OriginClass(raw["origin"])
        raw["assembly_mode"] = AssemblyMode(raw["assembly_mode"])
        if not isinstance(raw["component_names"], list):
            raise ValueError("SourceSchema.component_names must be an array")
        raw["component_names"] = tuple(raw["component_names"])
        raw["vertical_support"] = (
            VerticalSupport.from_dict(raw["vertical_support"])
            if raw["vertical_support"] is not None else None)
        raw["grid"] = (GridDescriptor.from_dict(raw["grid"])
                       if raw["grid"] is not None else None)
        raw["native_resolution"] = (
            SpatialScale.from_dict(raw["native_resolution"])
            if raw["native_resolution"] is not None else None)
        raw["missingness"] = Missingness.from_dict(raw["missingness"])
        raw["intrinsic_uncertainty"] = IntrinsicUncertainty.from_dict(
            raw["intrinsic_uncertainty"])
        return cls(**raw)


__all__ = ["AssemblyMode", "SourceSchema"]
