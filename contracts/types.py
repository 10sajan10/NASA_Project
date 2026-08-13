"""Intrinsic scientific artifact descriptors and their support types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .identity import (
    ScientificIdentity,
    canonical_crs,
    canonical_decimal,
    canonical_timestamp,
    canonical_unit,
    decimal_value,
    require_exact_fields,
    require_sequence,
    required_text,
    sorted_unique_text,
    timestamp_value,
)


class ScaleBasis(str, Enum):
    LINEAR = "LINEAR"
    ANGULAR = "ANGULAR"
    INDEX = "INDEX"


class TemporalKind(str, Enum):
    TIME_INVARIANT = "TIME_INVARIANT"
    SERIES = "SERIES"


class SampleSemantics(str, Enum):
    INSTANTANEOUS = "INSTANTANEOUS"
    MEAN = "MEAN"
    ACCUMULATION = "ACCUMULATION"
    MAXIMUM = "MAXIMUM"
    MINIMUM = "MINIMUM"


class VerticalKind(str, Enum):
    HEIGHT_AGL = "HEIGHT_AGL"
    HEIGHT_MSL = "HEIGHT_MSL"
    PRESSURE = "PRESSURE"
    MODEL_LEVEL = "MODEL_LEVEL"


class OriginClass(str, Enum):
    OBSERVATION = "OBSERVATION"
    ANALYSIS = "ANALYSIS"
    REANALYSIS = "REANALYSIS"
    FORECAST = "FORECAST"
    MODEL = "MODEL"
    DERIVED = "DERIVED"
    SYNTHETIC = "SYNTHETIC"


class MissingnessStatus(str, Enum):
    COMPLETE = "COMPLETE"
    BOUNDED = "BOUNDED"
    UNKNOWN = "UNKNOWN"


class UncertaintyStatus(str, Enum):
    """Knowledge state for uncertainty metadata, never a numeric sentinel."""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class SpatialScale(ScientificIdentity):
    x: str
    y: str
    unit: str
    basis: ScaleBasis = ScaleBasis.LINEAR

    identity_schema = "spatial-scale-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.basis, ScaleBasis):
            raise TypeError("spatial scale requires typed basis")
        x = canonical_decimal(self.x, "scale x")
        y = canonical_decimal(self.y, "scale y")
        unit = canonical_unit(self.unit)
        # This is coordinate-metadata normalization, not a payload transform.
        # It makes 1 km and 1000 m the same scale identity while deliberately
        # refusing any angular-to-linear approximation.
        if self.basis is ScaleBasis.LINEAR and unit == "km":
            x = canonical_decimal(decimal_value(x) * decimal_value("1000"))
            y = canonical_decimal(decimal_value(y) * decimal_value("1000"))
            unit = "m"
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "unit", unit)
        if decimal_value(self.x) <= 0 or decimal_value(self.y) <= 0:
            raise ValueError("spatial scale values must be positive")
        expected = {
            ScaleBasis.LINEAR: {"m", "km"},
            ScaleBasis.ANGULAR: {"degree"},
            ScaleBasis.INDEX: {"1"},
        }[self.basis]
        if self.unit not in expected:
            raise ValueError(f"unit {self.unit!r} is invalid for {self.basis.value}")

    @classmethod
    def isotropic(cls, value: object, unit: str = "m",
                  basis: ScaleBasis = ScaleBasis.LINEAR) -> "SpatialScale":
        token = canonical_decimal(value)
        return cls(token, token, unit, basis)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SpatialScale":
        value = require_exact_fields(
            value, ("x", "y", "unit", "basis"), "SpatialScale")
        return cls(
            x=value["x"], y=value["y"], unit=value["unit"],
            basis=ScaleBasis(value["basis"]),
        )


@dataclass(frozen=True)
class BBoxSupport(ScientificIdentity):
    crs: str
    axis_order: tuple[str, str]
    bounds: tuple[str, str, str, str]
    coverage_complete: bool = True

    identity_schema = "bbox-support-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "crs", canonical_crs(self.crs))
        if len(self.axis_order) != 2:
            raise ValueError("axis_order must contain exactly two axes")
        axes = tuple(required_text(axis, "axis name") for axis in self.axis_order)
        if axes[0] == axes[1]:
            raise ValueError("spatial axes must be distinct")
        object.__setattr__(self, "axis_order", axes)
        if len(self.bounds) != 4:
            raise ValueError("bounds must contain xmin, ymin, xmax, ymax")
        normalized = tuple(canonical_decimal(item, "bbox bound")
                           for item in self.bounds)
        if (decimal_value(normalized[0]) >= decimal_value(normalized[2])
                or decimal_value(normalized[1]) >= decimal_value(normalized[3])):
            raise ValueError("bbox minimums must be smaller than maximums")
        object.__setattr__(self, "bounds", normalized)
        if type(self.coverage_complete) is not bool:
            raise TypeError("coverage_complete must be bool")

    def contains(self, other: "BBoxSupport") -> bool:
        if (self.crs != other.crs or self.axis_order != other.axis_order
                or not self.coverage_complete):
            return False
        mine = tuple(decimal_value(item) for item in self.bounds)
        theirs = tuple(decimal_value(item) for item in other.bounds)
        return (mine[0] <= theirs[0] and mine[1] <= theirs[1]
                and mine[2] >= theirs[2] and mine[3] >= theirs[3])

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BBoxSupport":
        value = require_exact_fields(
            value,
            ("crs", "axis_order", "bounds", "coverage_complete"),
            "BBoxSupport",
        )
        return cls(
            value["crs"],
            require_sequence(value["axis_order"], "BBoxSupport.axis_order"),
            require_sequence(value["bounds"], "BBoxSupport.bounds"),
            value["coverage_complete"],
        )


@dataclass(frozen=True)
class GridDescriptor(ScientificIdentity):
    crs: str
    axis_order: tuple[str, str]
    shape: tuple[int, int]
    affine: tuple[str, str, str, str, str, str]
    spacing: SpatialScale

    identity_schema = "grid-descriptor-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.spacing, SpatialScale):
            raise TypeError("grid spacing must be a typed SpatialScale")
        object.__setattr__(self, "crs", canonical_crs(self.crs))
        if len(self.axis_order) != 2 or self.axis_order[0] == self.axis_order[1]:
            raise ValueError("grid requires two distinct ordered axes")
        object.__setattr__(self, "axis_order", tuple(
            required_text(axis, "grid axis") for axis in self.axis_order))
        if (len(self.shape) != 2 or any(type(item) is not int or item <= 0
                                       for item in self.shape)):
            raise ValueError("grid shape must contain two positive integers")
        if len(self.affine) != 6:
            raise ValueError("grid affine must contain six values")
        object.__setattr__(self, "affine", tuple(
            canonical_decimal(item, "affine value") for item in self.affine))

    @property
    def grid_id(self) -> str:
        return self.identity

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "GridDescriptor":
        value = require_exact_fields(
            value,
            ("crs", "axis_order", "shape", "affine", "spacing"),
            "GridDescriptor",
        )
        return cls(
            value["crs"],
            require_sequence(value["axis_order"], "GridDescriptor.axis_order"),
            require_sequence(value["shape"], "GridDescriptor.shape"),
            require_sequence(value["affine"], "GridDescriptor.affine"),
            SpatialScale.from_dict(value["spacing"]),
        )


@dataclass(frozen=True)
class TemporalSupport(ScientificIdentity):
    kind: TemporalKind
    start: str | None = None
    end: str | None = None
    cadence_s: str | None = None
    anchor: str | None = None
    max_gap_s: str | None = None
    sample_semantics: SampleSemantics = SampleSemantics.INSTANTANEOUS
    reference_time: str | None = None

    identity_schema = "temporal-support-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TemporalKind):
            raise TypeError("temporal support requires typed kind")
        if not isinstance(self.sample_semantics, SampleSemantics):
            raise TypeError("temporal support requires typed sample semantics")
        if self.kind is TemporalKind.TIME_INVARIANT:
            if any(value is not None for value in (
                    self.start, self.end, self.cadence_s, self.anchor,
                    self.max_gap_s, self.reference_time)):
                raise ValueError("time-invariant support cannot declare a timeline")
            return
        if self.start is None or self.end is None:
            raise ValueError("series support requires start and end")
        start = canonical_timestamp(self.start, "temporal start")
        end = canonical_timestamp(self.end, "temporal end")
        if timestamp_value(start) >= timestamp_value(end):
            raise ValueError("temporal start must precede end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.cadence_s is not None:
            cadence = canonical_decimal(self.cadence_s, "cadence")
            if decimal_value(cadence) <= 0:
                raise ValueError("cadence must be positive")
            object.__setattr__(self, "cadence_s", cadence)
        if self.anchor is not None:
            object.__setattr__(self, "anchor", canonical_timestamp(self.anchor))
        if self.max_gap_s is not None:
            gap = canonical_decimal(self.max_gap_s, "maximum gap")
            if decimal_value(gap) < 0:
                raise ValueError("maximum gap cannot be negative")
            object.__setattr__(self, "max_gap_s", gap)
        if self.reference_time is not None:
            object.__setattr__(self, "reference_time",
                               canonical_timestamp(self.reference_time))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TemporalSupport":
        value = require_exact_fields(
            value,
            (
                "kind", "start", "end", "cadence_s", "anchor", "max_gap_s",
                "sample_semantics", "reference_time",
            ),
            "TemporalSupport",
        )
        return cls(
            kind=TemporalKind(value["kind"]), start=value["start"],
            end=value["end"], cadence_s=value["cadence_s"],
            anchor=value["anchor"], max_gap_s=value["max_gap_s"],
            sample_semantics=SampleSemantics(value["sample_semantics"]),
            reference_time=value["reference_time"],
        )


@dataclass(frozen=True)
class VerticalSupport(ScientificIdentity):
    kind: VerticalKind
    unit: str
    datum: str
    levels: tuple[str, ...]

    identity_schema = "vertical-support-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VerticalKind):
            raise TypeError("vertical support requires typed kind")
        object.__setattr__(self, "unit", canonical_unit(self.unit))
        object.__setattr__(self, "datum", required_text(self.datum, "vertical datum"))
        if not self.levels:
            raise ValueError("vertical support requires at least one discrete level")
        normalized = tuple(canonical_decimal(value, "vertical level")
                           for value in self.levels)
        if len(set(normalized)) != len(normalized):
            raise ValueError("vertical levels cannot contain duplicates")
        object.__setattr__(self, "levels", tuple(sorted(
            normalized, key=decimal_value)))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VerticalSupport":
        value = require_exact_fields(
            value, ("kind", "unit", "datum", "levels"), "VerticalSupport")
        return cls(
            VerticalKind(value["kind"]), value["unit"], value["datum"],
            require_sequence(value["levels"], "VerticalSupport.levels"),
        )


@dataclass(frozen=True)
class Missingness(ScientificIdentity):
    status: MissingnessStatus
    max_fraction: str | None = None
    encodings: tuple[str, ...] = ()

    identity_schema = "missingness-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.status, MissingnessStatus):
            raise TypeError("missingness requires typed status")
        object.__setattr__(self, "encodings", sorted_unique_text(
            self.encodings, "missing-value encodings"))
        if self.status is MissingnessStatus.COMPLETE:
            if self.max_fraction not in (None, 0, "0"):
                raise ValueError("complete data has zero missing fraction")
            object.__setattr__(self, "max_fraction", "0")
        elif self.status is MissingnessStatus.BOUNDED:
            if self.max_fraction is None:
                raise ValueError("bounded missingness requires max_fraction")
            fraction = canonical_decimal(self.max_fraction, "missing fraction")
            if not DecimalRange.unit_interval(fraction):
                raise ValueError("missing fraction must be between zero and one")
            object.__setattr__(self, "max_fraction", fraction)
        elif self.max_fraction is not None:
            raise ValueError("unknown missingness cannot declare a bound")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Missingness":
        value = require_exact_fields(
            value, ("status", "max_fraction", "encodings"), "Missingness")
        return cls(
            MissingnessStatus(value["status"]), value["max_fraction"],
            require_sequence(value["encodings"], "Missingness.encodings"),
        )


class DecimalRange:
    @staticmethod
    def unit_interval(value: object) -> bool:
        number = decimal_value(value)
        return DecimalRange._zero() <= number <= DecimalRange._one()

    @staticmethod
    def _zero():
        return decimal_value("0")

    @staticmethod
    def _one():
        return decimal_value("1")


@dataclass(frozen=True)
class IntrinsicUncertainty(ScientificIdentity):
    """Uncertainty model intrinsic to the artifact's scientific values.

    A known record points to a versioned model and its immutable parameter
    manifest instead of pretending that every scalar, vector, or gridded field
    can be summarized by one interval.  Unknown and not-applicable states are
    explicit and carry a reason but no invented model references.
    """

    status: UncertaintyStatus
    uncertainty_model_id: str | None = None
    parameter_manifest_id: str | None = None
    reason: str | None = None

    identity_schema = "intrinsic-uncertainty-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.status, UncertaintyStatus):
            raise TypeError("intrinsic uncertainty requires typed status")
        if self.status is UncertaintyStatus.KNOWN:
            if self.uncertainty_model_id is None or self.parameter_manifest_id is None:
                raise ValueError(
                    "known intrinsic uncertainty requires model and parameter manifest")
            object.__setattr__(self, "uncertainty_model_id", required_text(
                self.uncertainty_model_id, "uncertainty_model_id"))
            object.__setattr__(self, "parameter_manifest_id", required_text(
                self.parameter_manifest_id, "parameter_manifest_id"))
            if self.reason is not None:
                raise ValueError("known intrinsic uncertainty cannot carry a reason")
            return
        if self.uncertainty_model_id is not None or self.parameter_manifest_id is not None:
            raise ValueError(
                "unknown/not-applicable intrinsic uncertainty cannot carry model references")
        object.__setattr__(self, "reason", required_text(
            self.reason or "", "intrinsic uncertainty reason"))

    @classmethod
    def known(
        cls,
        uncertainty_model_id: str,
        parameter_manifest_id: str,
    ) -> "IntrinsicUncertainty":
        return cls(
            UncertaintyStatus.KNOWN,
            uncertainty_model_id,
            parameter_manifest_id,
        )

    @classmethod
    def unknown(cls, reason: str) -> "IntrinsicUncertainty":
        return cls(UncertaintyStatus.UNKNOWN, reason=reason)

    @classmethod
    def not_applicable(cls, reason: str) -> "IntrinsicUncertainty":
        return cls(UncertaintyStatus.NOT_APPLICABLE, reason=reason)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IntrinsicUncertainty":
        value = require_exact_fields(
            value,
            (
                "status", "uncertainty_model_id", "parameter_manifest_id",
                "reason",
            ),
            "IntrinsicUncertainty",
        )
        return cls(
            UncertaintyStatus(value["status"]),
            value["uncertainty_model_id"],
            value["parameter_manifest_id"],
            value["reason"],
        )


@dataclass(frozen=True)
class ArtifactDescriptor(ScientificIdentity):
    concept_id: str
    schema_version: str
    representation: str
    units: str
    spatial_support: BBoxSupport
    temporal_support: TemporalSupport
    vertical_support: VerticalSupport | None
    grid: GridDescriptor | None
    native_resolution: SpatialScale | None
    origin: OriginClass
    missingness: Missingness
    ensemble_member: str | None = None
    intrinsic_uncertainty: IntrinsicUncertainty = field(default_factory=lambda: (
        IntrinsicUncertainty.unknown("NOT_REPORTED_BY_PRODUCER")))

    identity_schema = "artifact-descriptor-v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "concept_id", required_text(
            self.concept_id, "concept_id"))
        object.__setattr__(self, "schema_version", required_text(
            self.schema_version, "schema_version"))
        object.__setattr__(self, "representation", required_text(
            self.representation, "representation"))
        object.__setattr__(self, "units", canonical_unit(self.units))
        for value, expected, label in (
            (self.spatial_support, BBoxSupport, "spatial support"),
            (self.temporal_support, TemporalSupport, "temporal support"),
            (self.missingness, Missingness, "missingness"),
            (self.intrinsic_uncertainty, IntrinsicUncertainty,
             "intrinsic uncertainty"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"artifact {label} has the wrong type")
        if self.vertical_support is not None and not isinstance(
                self.vertical_support, VerticalSupport):
            raise TypeError("artifact vertical support has the wrong type")
        if self.grid is not None and not isinstance(self.grid, GridDescriptor):
            raise TypeError("artifact grid has the wrong type")
        if self.native_resolution is not None and not isinstance(
                self.native_resolution, SpatialScale):
            raise TypeError("artifact native resolution has the wrong type")
        if not isinstance(self.origin, OriginClass):
            raise TypeError("artifact origin has the wrong type")
        if self.ensemble_member is not None:
            object.__setattr__(self, "ensemble_member", required_text(
                self.ensemble_member, "ensemble_member"))

    @property
    def descriptor_id(self) -> str:
        return self.identity

    @property
    def grid_spacing(self) -> SpatialScale | None:
        return self.grid.spacing if self.grid else None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactDescriptor":
        value = require_exact_fields(
            value,
            (
                "concept_id", "schema_version", "representation", "units",
                "spatial_support", "temporal_support", "vertical_support",
                "grid", "native_resolution", "origin", "missingness",
                "ensemble_member", "intrinsic_uncertainty",
            ),
            "ArtifactDescriptor",
        )
        return cls(
            concept_id=value["concept_id"], schema_version=value["schema_version"],
            representation=value["representation"], units=value["units"],
            spatial_support=BBoxSupport.from_dict(value["spatial_support"]),
            temporal_support=TemporalSupport.from_dict(value["temporal_support"]),
            vertical_support=(VerticalSupport.from_dict(value["vertical_support"])
                              if value["vertical_support"] is not None else None),
            grid=(GridDescriptor.from_dict(value["grid"])
                  if value["grid"] is not None else None),
            native_resolution=(SpatialScale.from_dict(value["native_resolution"])
                               if value["native_resolution"] is not None else None),
            origin=OriginClass(value["origin"]),
            missingness=Missingness.from_dict(value["missingness"]),
            ensemble_member=value["ensemble_member"],
            intrinsic_uncertainty=IntrinsicUncertainty.from_dict(
                value["intrinsic_uncertainty"]),
        )
