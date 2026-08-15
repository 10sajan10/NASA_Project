"""Consumer-side acceptance constraints for scientific artifacts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any

from .identity import (
    ScientificIdentity,
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
from .types import (
    BBoxSupport,
    GridDescriptor,
    OriginClass,
    SampleSemantics,
    SpatialScale,
    TemporalKind,
    VerticalKind,
)


class ConstraintMode(str, Enum):
    ANY = "ANY"
    EXACT = "EXACT"
    ONE_OF = "ONE_OF"


class CadencePolicy(str, Enum):
    EXACT = "EXACT"
    AT_MOST = "AT_MOST"


class UnknownPolicy(str, Enum):
    FORBID = "FORBID"
    ALLOW_WITH_CAVEAT = "ALLOW_WITH_CAVEAT"


class CardinalityKind(str, Enum):
    EXACTLY = "EXACTLY"
    RANGE = "RANGE"


class DistinctnessPolicy(str, Enum):
    ALLOW_SAME = "ALLOW_SAME"
    DISTINCT_ARTIFACT = "DISTINCT_ARTIFACT"
    DISTINCT_PRODUCER = "DISTINCT_PRODUCER"


@dataclass(frozen=True)
class ValueConstraint(ScientificIdentity):
    mode: ConstraintMode = ConstraintMode.ANY
    values: tuple[str, ...] = ()

    identity_schema = "value-constraint-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ConstraintMode):
            raise TypeError("value constraint requires typed mode")
        object.__setattr__(self, "values", sorted_unique_text(
            self.values, "constraint values"))
        if self.mode is ConstraintMode.ANY and self.values:
            raise ValueError("ANY constraint cannot declare values")
        if self.mode is ConstraintMode.EXACT and len(self.values) != 1:
            raise ValueError("EXACT constraint requires one value")
        if self.mode is ConstraintMode.ONE_OF and not self.values:
            raise ValueError("ONE_OF constraint requires values")

    @classmethod
    def any(cls) -> "ValueConstraint":
        return cls()

    @classmethod
    def exact(cls, value: str) -> "ValueConstraint":
        return cls(ConstraintMode.EXACT, (value,))

    @classmethod
    def one_of(cls, *values: str) -> "ValueConstraint":
        return cls(ConstraintMode.ONE_OF, tuple(values))

    def accepts(self, value: str) -> bool:
        return self.mode is ConstraintMode.ANY or value in self.values

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ValueConstraint":
        value = require_exact_fields(
            value, ("mode", "values"), "ValueConstraint")
        return cls(
            ConstraintMode(value["mode"]),
            require_sequence(value["values"], "ValueConstraint.values"),
        )


@dataclass(frozen=True)
class SpatialRequirement(ScientificIdentity):
    support: BBoxSupport
    exact_grid: GridDescriptor | None = None
    max_grid_spacing: SpatialScale | None = None

    identity_schema = "spatial-requirement-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.support, BBoxSupport):
            raise TypeError("spatial requirement requires typed support")
        if self.exact_grid is not None and not isinstance(
                self.exact_grid, GridDescriptor):
            raise TypeError("exact grid must be a typed GridDescriptor")
        if self.max_grid_spacing is not None and not isinstance(
                self.max_grid_spacing, SpatialScale):
            raise TypeError("maximum grid spacing must be a typed SpatialScale")
        if self.exact_grid is not None and self.max_grid_spacing is not None:
            raise ValueError("choose exact_grid or max_grid_spacing, not both")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SpatialRequirement":
        value = require_exact_fields(
            value,
            ("support", "exact_grid", "max_grid_spacing"),
            "SpatialRequirement",
        )
        return cls(
            BBoxSupport.from_dict(value["support"]),
            GridDescriptor.from_dict(value["exact_grid"])
            if value["exact_grid"] is not None else None,
            SpatialScale.from_dict(value["max_grid_spacing"])
            if value["max_grid_spacing"] is not None else None,
        )


@dataclass(frozen=True)
class TemporalRequirement(ScientificIdentity):
    kind: TemporalKind
    start: str | None = None
    end: str | None = None
    cadence_s: str | None = None
    cadence_policy: CadencePolicy = CadencePolicy.AT_MOST
    anchor: str | None = None
    maximum_gap_s: str = "0"
    sample_semantics: SampleSemantics = SampleSemantics.INSTANTANEOUS
    reference_time: str | None = None

    identity_schema = "temporal-requirement-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TemporalKind):
            raise TypeError("temporal requirement requires typed kind")
        if not isinstance(self.cadence_policy, CadencePolicy):
            raise TypeError("temporal requirement requires typed cadence policy")
        if not isinstance(self.sample_semantics, SampleSemantics):
            raise TypeError("temporal requirement requires typed sample semantics")
        gap = canonical_decimal(self.maximum_gap_s, "maximum allowed gap")
        if decimal_value(gap) < 0:
            raise ValueError("maximum allowed gap cannot be negative")
        object.__setattr__(self, "maximum_gap_s", gap)
        if self.kind is TemporalKind.TIME_INVARIANT:
            if any(value is not None for value in (
                    self.start, self.end, self.cadence_s, self.anchor,
                    self.reference_time)):
                raise ValueError("time-invariant requirement has no timeline")
            return
        if self.start is None or self.end is None:
            raise ValueError("series requirement requires start and end")
        start = canonical_timestamp(self.start)
        end = canonical_timestamp(self.end)
        if timestamp_value(start) >= timestamp_value(end):
            raise ValueError("temporal requirement start must precede end")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.cadence_s is None:
            raise ValueError("series requirement requires cadence")
        cadence = canonical_decimal(self.cadence_s, "required cadence")
        if decimal_value(cadence) <= 0:
            raise ValueError("required cadence must be positive")
        object.__setattr__(self, "cadence_s", cadence)
        if self.anchor is not None:
            object.__setattr__(self, "anchor", canonical_timestamp(self.anchor))
        if self.reference_time is not None:
            object.__setattr__(self, "reference_time",
                               canonical_timestamp(self.reference_time))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TemporalRequirement":
        value = require_exact_fields(
            value,
            (
                "kind", "start", "end", "cadence_s", "cadence_policy",
                "anchor", "maximum_gap_s", "sample_semantics",
                "reference_time",
            ),
            "TemporalRequirement",
        )
        return cls(
            kind=TemporalKind(value["kind"]), start=value["start"],
            end=value["end"], cadence_s=value["cadence_s"],
            cadence_policy=CadencePolicy(value["cadence_policy"]),
            anchor=value["anchor"], maximum_gap_s=value["maximum_gap_s"],
            sample_semantics=SampleSemantics(value["sample_semantics"]),
            reference_time=value["reference_time"],
        )


@dataclass(frozen=True)
class VerticalRequirement(ScientificIdentity):
    kind: VerticalKind
    unit: str
    datum: str
    levels: tuple[str, ...]

    identity_schema = "vertical-requirement-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VerticalKind):
            raise TypeError("vertical requirement requires typed kind")
        object.__setattr__(self, "unit", canonical_unit(self.unit))
        object.__setattr__(self, "datum", required_text(self.datum, "vertical datum"))
        if not self.levels:
            raise ValueError("vertical requirement needs one or more exact levels")
        normalized = tuple(canonical_decimal(value, "required vertical level")
                           for value in self.levels)
        if len(set(normalized)) != len(normalized):
            raise ValueError("vertical requirement levels cannot repeat")
        object.__setattr__(self, "levels", tuple(sorted(
            normalized, key=decimal_value)))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VerticalRequirement":
        value = require_exact_fields(
            value, ("kind", "unit", "datum", "levels"),
            "VerticalRequirement")
        return cls(
            VerticalKind(value["kind"]), value["unit"], value["datum"],
            require_sequence(value["levels"], "VerticalRequirement.levels"),
        )


@dataclass(frozen=True)
class MissingPolicy(ScientificIdentity):
    max_fraction: str = "0"
    unknown_policy: UnknownPolicy = UnknownPolicy.FORBID
    accepted_encodings: tuple[str, ...] = ()

    identity_schema = "missing-policy-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.unknown_policy, UnknownPolicy):
            raise TypeError("missing policy requires typed unknown policy")
        fraction = canonical_decimal(self.max_fraction, "maximum missing fraction")
        if not (decimal_value("0") <= decimal_value(fraction)
                <= decimal_value("1")):
            raise ValueError("maximum missing fraction must be in [0,1]")
        object.__setattr__(self, "max_fraction", fraction)
        object.__setattr__(self, "accepted_encodings", sorted_unique_text(
            self.accepted_encodings, "accepted missing encodings"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MissingPolicy":
        value = require_exact_fields(
            value,
            ("max_fraction", "unknown_policy", "accepted_encodings"),
            "MissingPolicy",
        )
        return cls(
            value["max_fraction"], UnknownPolicy(value["unknown_policy"]),
            require_sequence(
                value["accepted_encodings"], "MissingPolicy.accepted_encodings"),
        )


@dataclass(frozen=True)
class EvidenceBound(ScientificIdentity):
    metric_definition_id: str
    maximum: str
    unit: str
    require_conservative_bound: bool = True

    identity_schema = "evidence-bound-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_definition_id", required_text(
            self.metric_definition_id, "metric_definition_id"))
        object.__setattr__(self, "maximum", canonical_decimal(
            self.maximum, "evidence maximum"))
        object.__setattr__(self, "unit", canonical_unit(self.unit))
        if type(self.require_conservative_bound) is not bool:
            raise TypeError("require_conservative_bound must be bool")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceBound":
        value = require_exact_fields(
            value,
            ("metric_definition_id", "maximum", "unit",
             "require_conservative_bound"),
            "EvidenceBound",
        )
        return cls(value["metric_definition_id"], value["maximum"], value["unit"],
                   value["require_conservative_bound"])


@dataclass(frozen=True)
class EvidenceRequirement(ScientificIdentity):
    required_profile_schema: str | None = None
    allow_unknown_empirical: bool = False
    required_metric_ids: tuple[str, ...] = ()

    identity_schema = "evidence-requirement-v1"

    def __post_init__(self) -> None:
        if self.required_profile_schema is not None:
            object.__setattr__(self, "required_profile_schema", required_text(
                self.required_profile_schema, "required evidence schema"))
        if type(self.allow_unknown_empirical) is not bool:
            raise TypeError("allow_unknown_empirical must be bool")
        object.__setattr__(self, "required_metric_ids", sorted_unique_text(
            self.required_metric_ids, "required metric IDs"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceRequirement":
        value = require_exact_fields(
            value,
            (
                "required_profile_schema", "allow_unknown_empirical",
                "required_metric_ids",
            ),
            "EvidenceRequirement",
        )
        return cls(
            value["required_profile_schema"], value["allow_unknown_empirical"],
            require_sequence(
                value["required_metric_ids"],
                "EvidenceRequirement.required_metric_ids",
            ),
        )


@dataclass(frozen=True)
class Cardinality(ScientificIdentity):
    kind: CardinalityKind = CardinalityKind.EXACTLY
    minimum: int = 1
    maximum: int = 1

    identity_schema = "cardinality-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CardinalityKind):
            raise TypeError("cardinality requires typed kind")
        if (type(self.minimum) is not int or type(self.maximum) is not int
                or self.minimum < 0 or self.maximum < self.minimum):
            raise ValueError("invalid cardinality bounds")
        if self.kind is CardinalityKind.EXACTLY and self.minimum != self.maximum:
            raise ValueError("EXACTLY cardinality requires equal bounds")

    @classmethod
    def exactly(cls, count: int) -> "Cardinality":
        return cls(CardinalityKind.EXACTLY, count, count)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Cardinality":
        value = require_exact_fields(
            value, ("kind", "minimum", "maximum"), "Cardinality")
        return cls(CardinalityKind(value["kind"]), value["minimum"], value["maximum"])


@dataclass(frozen=True)
class Requirement(ScientificIdentity):
    concept_id: str
    accepted_schema_versions: tuple[str, ...]
    representation: ValueConstraint
    units: ValueConstraint
    spatial: SpatialRequirement
    temporal: TemporalRequirement
    vertical: VerticalRequirement | None
    allowed_origins: tuple[OriginClass, ...]
    max_native_resolution: SpatialScale | None
    max_effective_resolution: EvidenceBound | None
    missing_policy: MissingPolicy
    minimum_evidence: EvidenceRequirement = field(default_factory=EvidenceRequirement)
    required_regimes: tuple[str, ...] = ()
    exact_descriptor_id: str | None = None

    identity_schema = "requirement-v3"

    def __post_init__(self) -> None:
        object.__setattr__(self, "concept_id", required_text(
            self.concept_id, "concept_id"))
        object.__setattr__(self, "accepted_schema_versions", sorted_unique_text(
            self.accepted_schema_versions, "accepted schema versions"))
        if not self.accepted_schema_versions:
            raise ValueError("at least one schema version must be accepted")
        for value, expected, label in (
            (self.representation, ValueConstraint, "representation"),
            (self.units, ValueConstraint, "units"),
            (self.spatial, SpatialRequirement, "spatial requirement"),
            (self.temporal, TemporalRequirement, "temporal requirement"),
            (self.missing_policy, MissingPolicy, "missing policy"),
            (self.minimum_evidence, EvidenceRequirement, "evidence requirement"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{label} has the wrong type")
        if self.vertical is not None and not isinstance(
                self.vertical, VerticalRequirement):
            raise TypeError("vertical requirement has the wrong type")
        if self.max_native_resolution is not None and not isinstance(
                self.max_native_resolution, SpatialScale):
            raise TypeError("native-resolution bound has the wrong type")
        if self.max_effective_resolution is not None and not isinstance(
                self.max_effective_resolution, EvidenceBound):
            raise TypeError("effective-resolution bound has the wrong type")
        if (not isinstance(self.allowed_origins, tuple)
                or not all(isinstance(value, OriginClass)
                           for value in self.allowed_origins)):
            raise TypeError("allowed origins must be typed OriginClass values")
        origins = tuple(sorted(set(self.allowed_origins), key=lambda item: item.value))
        if not origins:
            raise ValueError("at least one artifact origin must be allowed")
        object.__setattr__(self, "allowed_origins", origins)
        object.__setattr__(self, "required_regimes", sorted_unique_text(
            self.required_regimes, "required evidence regimes"))
        if self.exact_descriptor_id is not None:
            if (not isinstance(self.exact_descriptor_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", self.exact_descriptor_id)
                    is None):
                raise ValueError(
                    "exact_descriptor_id must be a lowercase SHA-256 digest")
        # Unit constraints normalize spelling without doing conversions.
        if self.units.mode is not ConstraintMode.ANY:
            object.__setattr__(self, "units", ValueConstraint(
                self.units.mode, tuple(canonical_unit(unit) for unit in self.units.values)))

    @property
    def requirement_id(self) -> str:
        return self.identity

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Requirement":
        value = require_exact_fields(
            value,
            (
                "concept_id", "accepted_schema_versions", "representation",
                "units", "spatial", "temporal", "vertical",
                "allowed_origins", "max_native_resolution",
                "max_effective_resolution", "missing_policy",
                "minimum_evidence", "required_regimes",
                "exact_descriptor_id",
            ),
            "Requirement",
        )
        return cls(
            concept_id=value["concept_id"],
            accepted_schema_versions=require_sequence(
                value["accepted_schema_versions"],
                "Requirement.accepted_schema_versions",
            ),
            representation=ValueConstraint.from_dict(value["representation"]),
            units=ValueConstraint.from_dict(value["units"]),
            spatial=SpatialRequirement.from_dict(value["spatial"]),
            temporal=TemporalRequirement.from_dict(value["temporal"]),
            vertical=(VerticalRequirement.from_dict(value["vertical"])
                      if value["vertical"] is not None else None),
            allowed_origins=tuple(OriginClass(item) for item in require_sequence(
                value["allowed_origins"], "Requirement.allowed_origins")),
            max_native_resolution=(SpatialScale.from_dict(value["max_native_resolution"])
                                   if value["max_native_resolution"] is not None else None),
            max_effective_resolution=(EvidenceBound.from_dict(
                value["max_effective_resolution"])
                if value["max_effective_resolution"] is not None else None),
            missing_policy=MissingPolicy.from_dict(value["missing_policy"]),
            minimum_evidence=EvidenceRequirement.from_dict(value["minimum_evidence"]),
            required_regimes=require_sequence(
                value["required_regimes"], "Requirement.required_regimes"),
            exact_descriptor_id=value["exact_descriptor_id"],
        )


@dataclass(frozen=True)
class RequirementUse(ScientificIdentity):
    use_id: str
    port_id: str
    requirement: Requirement
    optional: bool = False
    default_id: str | None = None
    cardinality: Cardinality = field(default_factory=Cardinality)
    distinctness: DistinctnessPolicy = DistinctnessPolicy.ALLOW_SAME
    shareable: bool = True

    identity_schema = "requirement-use-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "use_id", required_text(self.use_id, "use_id"))
        object.__setattr__(self, "port_id", required_text(self.port_id, "port_id"))
        if not isinstance(self.requirement, Requirement):
            raise TypeError("requirement use requires a typed Requirement")
        if not isinstance(self.cardinality, Cardinality):
            raise TypeError("requirement use requires typed Cardinality")
        if not isinstance(self.distinctness, DistinctnessPolicy):
            raise TypeError("requirement use requires typed DistinctnessPolicy")
        if type(self.optional) is not bool or type(self.shareable) is not bool:
            raise TypeError("optional and shareable must be bool")
        if self.default_id is not None:
            object.__setattr__(self, "default_id", required_text(
                self.default_id, "default_id"))
            if not self.optional:
                raise ValueError("only optional uses may declare a default")
        if not self.optional and self.cardinality.minimum == 0:
            raise ValueError("required use must require at least one satisfier")

    @property
    def requirement_use_id(self) -> str:
        return self.identity

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequirementUse":
        value = require_exact_fields(
            value,
            (
                "use_id", "port_id", "requirement", "optional", "default_id",
                "cardinality", "distinctness", "shareable",
            ),
            "RequirementUse",
        )
        return cls(
            value["use_id"], value["port_id"], Requirement.from_dict(value["requirement"]),
            value["optional"], value["default_id"],
            Cardinality.from_dict(value["cardinality"]),
            DistinctnessPolicy(value["distinctness"]), value["shareable"],
        )
