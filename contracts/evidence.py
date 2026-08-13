"""Versioned empirical evidence, applicability, and frozen evaluation policy."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
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
)
from .types import BBoxSupport, UncertaintyStatus, VerticalKind


class EvidenceStatus(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MetricComposition(str, Enum):
    ADDITIVE = "ADDITIVE"
    MAXIMUM = "MAXIMUM"
    BOTTLENECK = "BOTTLENECK"
    NON_COMPOSABLE = "NON_COMPOSABLE"


class BoundKind(str, Enum):
    POINT_ESTIMATE = "POINT_ESTIMATE"
    CONSERVATIVE_UPPER = "CONSERVATIVE_UPPER"


@dataclass(frozen=True)
class EvidenceSubject(ScientificIdentity):
    component_id: str
    component_version: str
    configuration_id: str
    output_port_id: str

    identity_schema = "evidence-subject-v1"

    def __post_init__(self) -> None:
        for name in ("component_id", "component_version", "configuration_id",
                     "output_port_id"):
            object.__setattr__(self, name, required_text(getattr(self, name), name))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceSubject":
        value = require_exact_fields(
            value,
            (
                "component_id", "component_version", "configuration_id",
                "output_port_id",
            ),
            "EvidenceSubject",
        )
        return cls(**value)


@dataclass(frozen=True)
class EvidenceApplicability(ScientificIdentity):
    spatial: BBoxSupport
    start: str
    end: str
    vertical_kind: VerticalKind | None = None
    vertical_unit: str | None = None
    vertical_datum: str | None = None
    vertical_levels: tuple[str, ...] = ()
    regimes: tuple[str, ...] = ()

    identity_schema = "evidence-applicability-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.spatial, BBoxSupport):
            raise TypeError("evidence applicability requires typed spatial support")
        if self.vertical_kind is not None and not isinstance(
                self.vertical_kind, VerticalKind):
            raise TypeError("evidence applicability requires typed vertical kind")
        object.__setattr__(self, "start", canonical_timestamp(self.start))
        object.__setattr__(self, "end", canonical_timestamp(self.end))
        from .identity import timestamp_value
        if timestamp_value(self.start) >= timestamp_value(self.end):
            raise ValueError("evidence applicability start must precede end")
        if self.vertical_kind is None:
            if any((self.vertical_unit, self.vertical_datum, self.vertical_levels)):
                raise ValueError("vertical applicability fields require vertical_kind")
        else:
            if self.vertical_unit is None or self.vertical_datum is None:
                raise ValueError("vertical applicability requires unit and datum")
            object.__setattr__(self, "vertical_unit", canonical_unit(self.vertical_unit))
            object.__setattr__(self, "vertical_datum", required_text(
                self.vertical_datum, "vertical datum"))
            levels = tuple(canonical_decimal(level, "vertical evidence level")
                           for level in self.vertical_levels)
            object.__setattr__(self, "vertical_levels", tuple(sorted(
                set(levels), key=decimal_value)))
        object.__setattr__(self, "regimes", sorted_unique_text(
            self.regimes, "evidence regimes"))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceApplicability":
        value = require_exact_fields(
            value,
            (
                "spatial", "start", "end", "vertical_kind", "vertical_unit",
                "vertical_datum", "vertical_levels", "regimes",
            ),
            "EvidenceApplicability",
        )
        return cls(
            spatial=BBoxSupport.from_dict(value["spatial"]), start=value["start"],
            end=value["end"],
            vertical_kind=(VerticalKind(value["vertical_kind"])
                           if value["vertical_kind"] is not None else None),
            vertical_unit=value["vertical_unit"],
            vertical_datum=value["vertical_datum"],
            vertical_levels=require_sequence(
                value["vertical_levels"],
                "EvidenceApplicability.vertical_levels",
            ),
            regimes=require_sequence(
                value["regimes"], "EvidenceApplicability.regimes"),
        )


@dataclass(frozen=True)
class MetricDefinition(ScientificIdentity):
    metric_definition_id: str
    version: str
    unit: str
    method_id: str
    composition: MetricComposition = MetricComposition.NON_COMPOSABLE

    identity_schema = "metric-definition-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.composition, MetricComposition):
            raise TypeError("metric definition requires typed composition")
        for name in ("metric_definition_id", "version", "method_id"):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        object.__setattr__(self, "unit", canonical_unit(self.unit))

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MetricDefinition":
        value = require_exact_fields(
            value,
            (
                "metric_definition_id", "version", "unit", "method_id",
                "composition",
            ),
            "MetricDefinition",
        )
        return cls(
            value["metric_definition_id"], value["version"], value["unit"],
            value["method_id"], MetricComposition(value["composition"]),
        )


@dataclass(frozen=True)
class EstimateUncertainty(ScientificIdentity):
    """Uncertainty of an empirical metric estimate, not of the artifact.

    Known uncertainty is a confidence interval produced by an identified
    method.  Unknown and not-applicable records contain no numeric placeholders
    and must explain why an interval is absent.
    """

    status: UncertaintyStatus
    lower_bound: str | None = None
    upper_bound: str | None = None
    unit: str | None = None
    confidence_level: str | None = None
    method_id: str | None = None
    reason: str | None = None

    identity_schema = "estimate-uncertainty-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.status, UncertaintyStatus):
            raise TypeError("estimate uncertainty requires typed status")
        if self.status is UncertaintyStatus.KNOWN:
            if any(value is None for value in (
                    self.lower_bound, self.upper_bound, self.unit,
                    self.confidence_level, self.method_id)):
                raise ValueError(
                    "known estimate uncertainty requires interval, confidence, and method")
            lower = canonical_decimal(self.lower_bound, "uncertainty lower bound")
            upper = canonical_decimal(self.upper_bound, "uncertainty upper bound")
            if decimal_value(lower) > decimal_value(upper):
                raise ValueError("uncertainty lower bound cannot exceed upper bound")
            confidence = canonical_decimal(
                self.confidence_level, "uncertainty confidence level")
            if not (decimal_value("0") < decimal_value(confidence)
                    <= decimal_value("1")):
                raise ValueError("uncertainty confidence level must be in (0,1]")
            object.__setattr__(self, "lower_bound", lower)
            object.__setattr__(self, "upper_bound", upper)
            object.__setattr__(self, "unit", canonical_unit(self.unit))
            object.__setattr__(self, "confidence_level", confidence)
            object.__setattr__(self, "method_id", required_text(
                self.method_id, "uncertainty method_id"))
            if self.reason is not None:
                raise ValueError("known estimate uncertainty cannot carry a reason")
            return
        if any(value is not None for value in (
                self.lower_bound, self.upper_bound, self.unit,
                self.confidence_level, self.method_id)):
            raise ValueError(
                "unknown/not-applicable estimate uncertainty cannot carry an interval")
        object.__setattr__(self, "reason", required_text(
            self.reason or "", "estimate uncertainty reason"))

    @classmethod
    def known(
        cls,
        lower_bound: object,
        upper_bound: object,
        unit: str,
        confidence_level: object,
        method_id: str,
    ) -> "EstimateUncertainty":
        return cls(
            UncertaintyStatus.KNOWN,
            canonical_decimal(lower_bound),
            canonical_decimal(upper_bound),
            unit,
            canonical_decimal(confidence_level),
            method_id,
        )

    @classmethod
    def unknown(cls, reason: str) -> "EstimateUncertainty":
        return cls(UncertaintyStatus.UNKNOWN, reason=reason)

    @classmethod
    def not_applicable(cls, reason: str) -> "EstimateUncertainty":
        return cls(UncertaintyStatus.NOT_APPLICABLE, reason=reason)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EstimateUncertainty":
        value = require_exact_fields(
            value,
            (
                "status", "lower_bound", "upper_bound", "unit",
                "confidence_level", "method_id", "reason",
            ),
            "EstimateUncertainty",
        )
        return cls(
            UncertaintyStatus(value["status"]),
            value["lower_bound"],
            value["upper_bound"],
            value["unit"],
            value["confidence_level"],
            value["method_id"],
            value["reason"],
        )


@dataclass(frozen=True)
class EvidenceClaim(ScientificIdentity):
    metric_definition_id: str
    status: EvidenceStatus
    unit: str
    value: str | None
    bound_kind: BoundKind | None
    reference_manifest_id: str | None
    protocol_id: str | None
    evaluator_id: str | None
    applicability: EvidenceApplicability | None
    reason: str | None = None
    estimate_uncertainty: EstimateUncertainty = field(default_factory=lambda: (
        EstimateUncertainty.unknown("NOT_REPORTED_BY_EVIDENCE_PROVIDER")))

    identity_schema = "evidence-claim-v2"

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_definition_id", required_text(
            self.metric_definition_id, "metric_definition_id"))
        object.__setattr__(self, "unit", canonical_unit(self.unit))
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("evidence claim requires typed status")
        if self.bound_kind is not None and not isinstance(
                self.bound_kind, BoundKind):
            raise TypeError("evidence claim requires typed bound kind")
        if self.applicability is not None and not isinstance(
                self.applicability, EvidenceApplicability):
            raise TypeError("evidence claim requires typed applicability")
        if not isinstance(self.estimate_uncertainty, EstimateUncertainty):
            raise TypeError("evidence claim requires typed estimate uncertainty")
        if self.status is EvidenceStatus.KNOWN:
            if any(value is None for value in (
                    self.value, self.bound_kind, self.reference_manifest_id,
                    self.protocol_id, self.evaluator_id, self.applicability)):
                raise ValueError("known evidence requires value, method, reference, and applicability")
            object.__setattr__(self, "value", canonical_decimal(
                self.value, "evidence value"))
            for name in ("reference_manifest_id", "protocol_id", "evaluator_id"):
                object.__setattr__(self, name, required_text(getattr(self, name), name))
            if self.reason is not None:
                raise ValueError("known evidence cannot have an unknown reason")
            if (self.estimate_uncertainty.status is UncertaintyStatus.KNOWN
                    and self.estimate_uncertainty.unit != self.unit):
                raise ValueError(
                    "estimate uncertainty interval unit must match claim unit")
        else:
            if any(value is not None for value in (
                    self.value, self.bound_kind, self.reference_manifest_id,
                    self.protocol_id, self.evaluator_id, self.applicability)):
                raise ValueError("unknown/not-applicable evidence cannot carry a numeric claim")
            object.__setattr__(self, "reason", required_text(self.reason or "", "evidence reason"))
            if self.estimate_uncertainty.status is UncertaintyStatus.KNOWN:
                raise ValueError(
                    "a claim without a numeric estimate cannot have known estimate uncertainty")

    @classmethod
    def known(cls, metric_definition_id: str, value: object, unit: str,
              *, bound_kind: BoundKind, reference_manifest_id: str,
              protocol_id: str, evaluator_id: str,
              applicability: EvidenceApplicability,
              estimate_uncertainty: EstimateUncertainty | None = None,
              ) -> "EvidenceClaim":
        uncertainty = (estimate_uncertainty
                       if estimate_uncertainty is not None
                       else EstimateUncertainty.unknown(
                           "NOT_REPORTED_BY_EVIDENCE_PROVIDER"))
        return cls(metric_definition_id, EvidenceStatus.KNOWN, unit,
                   canonical_decimal(value), bound_kind, reference_manifest_id,
                   protocol_id, evaluator_id, applicability,
                   estimate_uncertainty=uncertainty)

    @classmethod
    def unknown(cls, metric_definition_id: str, unit: str,
                reason: str) -> "EvidenceClaim":
        return cls(
            metric_definition_id, EvidenceStatus.UNKNOWN, unit, None,
            None, None, None, None, None, reason,
            EstimateUncertainty.not_applicable("NO_NUMERIC_ESTIMATE"),
        )

    @classmethod
    def not_applicable(cls, metric_definition_id: str, unit: str,
                       reason: str) -> "EvidenceClaim":
        return cls(
            metric_definition_id, EvidenceStatus.NOT_APPLICABLE, unit, None,
            None, None, None, None, None, reason,
            EstimateUncertainty.not_applicable("NO_APPLICABLE_ESTIMATE"),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceClaim":
        value = require_exact_fields(
            value,
            (
                "metric_definition_id", "status", "unit", "value",
                "bound_kind", "reference_manifest_id", "protocol_id",
                "evaluator_id", "applicability", "reason",
                "estimate_uncertainty",
            ),
            "EvidenceClaim",
        )
        return cls(
            metric_definition_id=value["metric_definition_id"],
            status=EvidenceStatus(value["status"]), unit=value["unit"],
            value=value["value"],
            bound_kind=(BoundKind(value["bound_kind"])
                        if value["bound_kind"] is not None else None),
            reference_manifest_id=value["reference_manifest_id"],
            protocol_id=value["protocol_id"], evaluator_id=value["evaluator_id"],
            applicability=(EvidenceApplicability.from_dict(value["applicability"])
                           if value["applicability"] is not None else None),
            reason=value["reason"],
            estimate_uncertainty=EstimateUncertainty.from_dict(
                value["estimate_uncertainty"]),
        )


@dataclass(frozen=True)
class EvidenceProfile(ScientificIdentity):
    schema_version: str
    subject: EvidenceSubject
    claims: tuple[EvidenceClaim, ...]
    factual_metadata_ids: tuple[str, ...] = ()

    identity_schema = "evidence-profile-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.subject, EvidenceSubject):
            raise TypeError("evidence profile requires typed subject")
        if (not isinstance(self.claims, tuple)
                or not all(isinstance(claim, EvidenceClaim)
                           for claim in self.claims)):
            raise TypeError("evidence profile requires typed claim tuple")
        object.__setattr__(self, "schema_version", required_text(
            self.schema_version, "evidence schema_version"))
        if len({claim.metric_definition_id for claim in self.claims}) != len(self.claims):
            raise ValueError("evidence profile cannot repeat a metric")
        object.__setattr__(self, "claims", tuple(sorted(
            self.claims, key=lambda claim: claim.metric_definition_id)))
        object.__setattr__(self, "factual_metadata_ids", sorted_unique_text(
            self.factual_metadata_ids, "factual metadata IDs"))

    @property
    def profile_id(self) -> str:
        return self.identity

    def claim_for(self, metric_definition_id: str) -> EvidenceClaim | None:
        return next((claim for claim in self.claims
                     if claim.metric_definition_id == metric_definition_id), None)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceProfile":
        value = require_exact_fields(
            value,
            ("schema_version", "subject", "claims", "factual_metadata_ids"),
            "EvidenceProfile",
        )
        return cls(
            value["schema_version"], EvidenceSubject.from_dict(value["subject"]),
            tuple(EvidenceClaim.from_dict(item) for item in require_sequence(
                value["claims"], "EvidenceProfile.claims")),
            require_sequence(
                value["factual_metadata_ids"],
                "EvidenceProfile.factual_metadata_ids",
            ),
        )


@dataclass(frozen=True)
class MetricEvaluator(ScientificIdentity):
    evaluator_id: str
    version: str
    metric_definitions: tuple[MetricDefinition, ...]

    identity_schema = "metric-evaluator-v1"

    def __post_init__(self) -> None:
        if (not isinstance(self.metric_definitions, tuple)
                or not all(isinstance(item, MetricDefinition)
                           for item in self.metric_definitions)):
            raise TypeError("metric evaluator requires typed definition tuple")
        object.__setattr__(self, "evaluator_id", required_text(
            self.evaluator_id, "evaluator_id"))
        object.__setattr__(self, "version", required_text(self.version, "evaluator version"))
        if len({item.metric_definition_id for item in self.metric_definitions}) != len(
                self.metric_definitions):
            raise ValueError("metric evaluator cannot repeat definitions")
        object.__setattr__(self, "metric_definitions", tuple(sorted(
            self.metric_definitions, key=lambda item: item.metric_definition_id)))

    def definition_for(self, metric_definition_id: str) -> MetricDefinition | None:
        return next((item for item in self.metric_definitions
                     if item.metric_definition_id == metric_definition_id), None)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MetricEvaluator":
        value = require_exact_fields(
            value,
            ("evaluator_id", "version", "metric_definitions"),
            "MetricEvaluator",
        )
        return cls(value["evaluator_id"], value["version"], tuple(
            MetricDefinition.from_dict(item) for item in require_sequence(
                value["metric_definitions"],
                "MetricEvaluator.metric_definitions",
            )))


@dataclass(frozen=True)
class EvidenceSnapshot(ScientificIdentity):
    captured_at: str
    evaluator: MetricEvaluator
    profiles: tuple[EvidenceProfile, ...]

    identity_schema = "evidence-snapshot-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.evaluator, MetricEvaluator):
            raise TypeError("evidence snapshot requires typed evaluator")
        if (not isinstance(self.profiles, tuple)
                or not all(isinstance(profile, EvidenceProfile)
                           for profile in self.profiles)):
            raise TypeError("evidence snapshot requires typed profile tuple")
        object.__setattr__(self, "captured_at", canonical_timestamp(self.captured_at))
        if len({profile.profile_id for profile in self.profiles}) != len(self.profiles):
            raise ValueError("evidence snapshot cannot repeat profiles")
        object.__setattr__(self, "profiles", tuple(sorted(
            self.profiles, key=lambda profile: profile.profile_id)))

    @property
    def snapshot_id(self) -> str:
        return self.identity

    def contains(self, profile: EvidenceProfile) -> bool:
        return any(item.profile_id == profile.profile_id for item in self.profiles)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceSnapshot":
        value = require_exact_fields(
            value, ("captured_at", "evaluator", "profiles"),
            "EvidenceSnapshot")
        return cls(
            value["captured_at"], MetricEvaluator.from_dict(value["evaluator"]),
            tuple(EvidenceProfile.from_dict(item) for item in require_sequence(
                value["profiles"], "EvidenceSnapshot.profiles")),
        )
