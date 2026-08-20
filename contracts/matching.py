"""Pure direct compatibility matching with complete structured proofs.

No function here discovers, inserts, or performs a transformation.  A failed
check may include a transformation *hint*, but a later graph builder must turn
that into an explicit capability invocation before it can satisfy a use.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable

from .evidence import (
    BoundKind,
    EvidenceApplicability,
    EvidenceProfile,
    EvidenceSnapshot,
    EvidenceStatus,
    EvidenceSubject,
)
from .identity import (
    ScientificIdentity,
    decimal_value,
    require_exact_fields,
    require_sequence,
    sorted_unique_text,
    timestamp_value,
)
from .requirements import CadencePolicy, ConstraintMode, Requirement
from .types import ArtifactDescriptor, MissingnessStatus, ScaleBasis, TemporalKind


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN_ALLOWED = "UNKNOWN_ALLOWED"
    NOT_EVALUATED = "NOT_EVALUATED"


class MatchCode(str, Enum):
    MATCH = "MATCH"
    DESCRIPTOR_IDENTITY_MISMATCH = "DESCRIPTOR_IDENTITY_MISMATCH"
    CONCEPT_MISMATCH = "CONCEPT_MISMATCH"
    SCHEMA_VERSION_MISMATCH = "SCHEMA_VERSION_MISMATCH"
    REPRESENTATION_MISMATCH = "REPRESENTATION_MISMATCH"
    UNITS_MISMATCH = "UNITS_MISMATCH"
    SPATIAL_CRS_MISMATCH = "SPATIAL_CRS_MISMATCH"
    SPATIAL_COVERAGE_GAP = "SPATIAL_COVERAGE_GAP"
    GRID_REQUIRED_MISSING = "GRID_REQUIRED_MISSING"
    GRID_IDENTITY_MISMATCH = "GRID_IDENTITY_MISMATCH"
    GRID_SPACING_TOO_COARSE = "GRID_SPACING_TOO_COARSE"
    GRID_SPACING_INCOMPARABLE = "GRID_SPACING_INCOMPARABLE"
    TIME_KIND_MISMATCH = "TIME_KIND_MISMATCH"
    TIME_SEMANTICS_MISMATCH = "TIME_SEMANTICS_MISMATCH"
    TEMPORAL_COVERAGE_GAP = "TEMPORAL_COVERAGE_GAP"
    CADENCE_UNKNOWN = "CADENCE_UNKNOWN"
    CADENCE_TOO_COARSE = "CADENCE_TOO_COARSE"
    CADENCE_NOT_EXACT = "CADENCE_NOT_EXACT"
    TIME_ALIGNMENT_MISMATCH = "TIME_ALIGNMENT_MISMATCH"
    TIME_REFERENCE_MISMATCH = "TIME_REFERENCE_MISMATCH"
    TEMPORAL_GAPS_UNKNOWN = "TEMPORAL_GAPS_UNKNOWN"
    TEMPORAL_GAPS_EXCEED_POLICY = "TEMPORAL_GAPS_EXCEED_POLICY"
    VERTICAL_REQUIRED_MISSING = "VERTICAL_REQUIRED_MISSING"
    VERTICAL_KIND_MISMATCH = "VERTICAL_KIND_MISMATCH"
    VERTICAL_DATUM_MISMATCH = "VERTICAL_DATUM_MISMATCH"
    VERTICAL_UNITS_MISMATCH = "VERTICAL_UNITS_MISMATCH"
    VERTICAL_LEVEL_MISSING = "VERTICAL_LEVEL_MISSING"
    ORIGIN_DISALLOWED = "ORIGIN_DISALLOWED"
    NATIVE_RESOLUTION_UNKNOWN = "NATIVE_RESOLUTION_UNKNOWN"
    NATIVE_RESOLUTION_INCOMPARABLE = "NATIVE_RESOLUTION_INCOMPARABLE"
    NATIVE_RESOLUTION_TOO_COARSE = "NATIVE_RESOLUTION_TOO_COARSE"
    MISSINGNESS_UNKNOWN = "MISSINGNESS_UNKNOWN"
    MISSINGNESS_EXCEEDS_POLICY = "MISSINGNESS_EXCEEDS_POLICY"
    MISSINGNESS_ENCODING_MISMATCH = "MISSINGNESS_ENCODING_MISMATCH"
    QUALITY_UNKNOWN_ALLOWED = "QUALITY_UNKNOWN_ALLOWED"
    EVIDENCE_PROFILE_MISSING = "EVIDENCE_PROFILE_MISSING"
    EVIDENCE_SCHEMA_MISMATCH = "EVIDENCE_SCHEMA_MISMATCH"
    EVIDENCE_SNAPSHOT_MISSING = "EVIDENCE_SNAPSHOT_MISSING"
    EVIDENCE_SNAPSHOT_MISMATCH = "EVIDENCE_SNAPSHOT_MISMATCH"
    EVIDENCE_SUBJECT_MISSING = "EVIDENCE_SUBJECT_MISSING"
    EVIDENCE_SUBJECT_MISMATCH = "EVIDENCE_SUBJECT_MISMATCH"
    EVIDENCE_CLAIM_UNKNOWN = "EVIDENCE_CLAIM_UNKNOWN"
    EVIDENCE_NOT_APPLICABLE = "EVIDENCE_NOT_APPLICABLE"
    EVIDENCE_METHOD_MISMATCH = "EVIDENCE_METHOD_MISMATCH"
    EVIDENCE_UNITS_MISMATCH = "EVIDENCE_UNITS_MISMATCH"
    EVIDENCE_BOUND_KIND_MISMATCH = "EVIDENCE_BOUND_KIND_MISMATCH"
    EVIDENCE_BOUND_FAILED = "EVIDENCE_BOUND_FAILED"


@dataclass(frozen=True)
class CompatibilityCheck(ScientificIdentity):
    dimension: str
    status: CheckStatus
    code: MatchCode
    detail: str = ""
    transformation_hint: str | None = None

    identity_schema = "compatibility-check-v1"

    @classmethod
    def from_dict(cls, value: dict) -> "CompatibilityCheck":
        value = require_exact_fields(
            value,
            ("dimension", "status", "code", "detail", "transformation_hint"),
            "CompatibilityCheck",
        )
        return cls(
            value["dimension"], CheckStatus(value["status"]),
            MatchCode(value["code"]), value["detail"],
            value["transformation_hint"],
        )


@dataclass(frozen=True)
class CompatibilityProof(ScientificIdentity):
    requirement_id: str
    descriptor_id: str
    evidence_profile_id: str | None
    evidence_snapshot_id: str | None
    evidence_subject_id: str | None
    checks: tuple[CompatibilityCheck, ...]

    identity_schema = "compatibility-proof-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id:
            raise ValueError("compatibility proof requires a requirement identity")
        if not isinstance(self.descriptor_id, str) or not self.descriptor_id:
            raise ValueError("compatibility proof requires a descriptor identity")
        if (not isinstance(self.checks, tuple) or not self.checks
                or not all(isinstance(value, CompatibilityCheck)
                           for value in self.checks)):
            raise ValueError(
                "compatibility proof requires a non-empty typed check set")
        dimensions = tuple(value.dimension for value in self.checks)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("compatibility proof check dimensions must be unique")

    @property
    def satisfied(self) -> bool:
        return bool(self.checks) and all(
            check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def rejection_codes(self) -> tuple[MatchCode, ...]:
        return tuple(check.code for check in self.checks
                     if check.status is CheckStatus.FAIL)

    @property
    def caveat_codes(self) -> tuple[MatchCode, ...]:
        return tuple(check.code for check in self.checks
                     if check.status is CheckStatus.UNKNOWN_ALLOWED)

    @property
    def proof_id(self) -> str:
        return self.identity

    @classmethod
    def from_dict(cls, value: dict) -> "CompatibilityProof":
        value = require_exact_fields(
            value,
            (
                "requirement_id", "descriptor_id", "evidence_profile_id",
                "evidence_snapshot_id", "evidence_subject_id", "checks",
            ),
            "CompatibilityProof",
        )
        return cls(
            value["requirement_id"], value["descriptor_id"],
            value["evidence_profile_id"], value["evidence_snapshot_id"],
            value["evidence_subject_id"],
            tuple(CompatibilityCheck.from_dict(item) for item in require_sequence(
                value["checks"], "CompatibilityProof.checks")),
        )


def _check(dimension: str, passed: bool, failure: MatchCode, detail: str = "",
           hint: str | None = None) -> CompatibilityCheck:
    return CompatibilityCheck(
        dimension, CheckStatus.PASS if passed else CheckStatus.FAIL,
        MatchCode.MATCH if passed else failure, detail, None if passed else hint)


def _scale_leq(actual, maximum) -> bool | None:
    if actual.basis is not maximum.basis or actual.unit != maximum.unit:
        return None
    return (decimal_value(actual.x) <= decimal_value(maximum.x)
            and decimal_value(actual.y) <= decimal_value(maximum.y))


def _integer_multiple(delta_s: Decimal, cadence_s: Decimal) -> bool:
    if cadence_s <= 0:
        return False
    return delta_s % cadence_s == 0


def _seconds_between(left: str, right: str) -> Decimal:
    return Decimal(str((timestamp_value(left) - timestamp_value(right)).total_seconds()))


def _applicability_contains(app: EvidenceApplicability, requirement: Requirement,
                            regimes: tuple[str, ...]) -> bool:
    temporal = requirement.temporal
    if temporal.start is None or temporal.end is None:
        return False
    if not app.spatial.contains(requirement.spatial.support):
        return False
    if (timestamp_value(app.start) > timestamp_value(temporal.start)
            or timestamp_value(app.end) < timestamp_value(temporal.end)):
        return False
    required_vertical = requirement.vertical
    if required_vertical is not None:
        if (app.vertical_kind != required_vertical.kind
                or app.vertical_unit != required_vertical.unit
                or app.vertical_datum != required_vertical.datum
                or not set(required_vertical.levels).issubset(app.vertical_levels)):
            return False
    return set(regimes).issubset(app.regimes)


def direct_match(
    descriptor: ArtifactDescriptor,
    requirement: Requirement,
    evidence_profile: EvidenceProfile | None = None,
    *,
    evidence_snapshot: EvidenceSnapshot | None = None,
    evidence_subject: EvidenceSubject | None = None,
    requested_regimes: Iterable[str] = (),
) -> CompatibilityProof:
    """Prove whether an artifact directly satisfies a requirement.

    The function evaluates every independent dimension in a stable order.  It
    is side-effect free and does not short circuit, so rejection diagnostics
    are complete and deterministic.
    """
    checks: list[CompatibilityCheck] = []
    if isinstance(requested_regimes, str):
        raise TypeError("requested_regimes must be an iterable of regime strings")
    try:
        supplied_regimes = tuple(requested_regimes)
    except TypeError as exc:
        raise TypeError(
            "requested_regimes must be an iterable of regime strings") from exc
    supplied_regimes = sorted_unique_text(
        supplied_regimes, "requested evidence regimes")
    if supplied_regimes and supplied_regimes != requirement.required_regimes:
        raise ValueError(
            "requested_regimes must be empty or exactly match "
            "Requirement.required_regimes")
    regimes = requirement.required_regimes

    checks.append(_check(
        "descriptor_identity",
        (requirement.exact_descriptor_id is None
         or descriptor.descriptor_id == requirement.exact_descriptor_id),
        MatchCode.DESCRIPTOR_IDENTITY_MISMATCH))
    checks.append(_check("concept", descriptor.concept_id == requirement.concept_id,
                         MatchCode.CONCEPT_MISMATCH))
    checks.append(_check(
        "schema_version",
        descriptor.schema_version in requirement.accepted_schema_versions,
        MatchCode.SCHEMA_VERSION_MISMATCH))
    checks.append(_check(
        "representation", requirement.representation.accepts(descriptor.representation),
        MatchCode.REPRESENTATION_MISMATCH,
        hint="representation-conversion"))
    checks.append(_check("units", requirement.units.accepts(descriptor.units),
                         MatchCode.UNITS_MISMATCH, hint="unit-conversion"))

    requested_space = requirement.spatial.support
    crs_match = (descriptor.spatial_support.crs == requested_space.crs
                 and descriptor.spatial_support.axis_order == requested_space.axis_order)
    checks.append(_check("spatial_crs", crs_match, MatchCode.SPATIAL_CRS_MISMATCH,
                         hint="reprojection"))
    coverage = crs_match and descriptor.spatial_support.contains(requested_space)
    checks.append(_check("spatial_coverage", coverage,
                         MatchCode.SPATIAL_COVERAGE_GAP, hint="mosaic-or-clip"))

    if requirement.spatial.exact_grid is not None:
        checks.append(_check("grid_present", descriptor.grid is not None,
                             MatchCode.GRID_REQUIRED_MISSING))
        checks.append(_check(
            "grid_identity",
            descriptor.grid is not None
            and descriptor.grid.grid_id == requirement.spatial.exact_grid.grid_id,
            MatchCode.GRID_IDENTITY_MISMATCH, hint="regrid"))
    if requirement.spatial.max_grid_spacing is not None:
        if descriptor.grid is None:
            checks.append(_check("grid_spacing", False,
                                 MatchCode.GRID_REQUIRED_MISSING))
        else:
            result = _scale_leq(descriptor.grid.spacing,
                                requirement.spatial.max_grid_spacing)
            checks.append(_check(
                "grid_spacing", result is True,
                (MatchCode.GRID_SPACING_INCOMPARABLE if result is None
                 else MatchCode.GRID_SPACING_TOO_COARSE), hint="regrid"))

    offered_time = descriptor.temporal_support
    requested_time = requirement.temporal
    kind_match = offered_time.kind is requested_time.kind
    checks.append(_check("temporal_kind", kind_match, MatchCode.TIME_KIND_MISMATCH))
    checks.append(_check(
        "temporal_semantics",
        kind_match and offered_time.sample_semantics is requested_time.sample_semantics,
        MatchCode.TIME_SEMANTICS_MISMATCH, hint="temporal-aggregation"))
    if requested_time.kind is TemporalKind.SERIES:
        coverage_ok = (
            offered_time.kind is TemporalKind.SERIES
            and offered_time.start is not None and offered_time.end is not None
            and timestamp_value(offered_time.start) <= timestamp_value(requested_time.start)
            and timestamp_value(offered_time.end) >= timestamp_value(requested_time.end))
        checks.append(_check("temporal_coverage", coverage_ok,
                             MatchCode.TEMPORAL_COVERAGE_GAP, hint="temporal-fetch"))
        if offered_time.cadence_s is None:
            checks.append(_check("cadence", False, MatchCode.CADENCE_UNKNOWN))
        else:
            offered_cadence = decimal_value(offered_time.cadence_s)
            required_cadence = decimal_value(requested_time.cadence_s)
            cadence_ok = (offered_cadence == required_cadence
                           if requested_time.cadence_policy is CadencePolicy.EXACT
                           else (offered_cadence <= required_cadence
                                 and required_cadence % offered_cadence == 0))
            code = (MatchCode.CADENCE_NOT_EXACT
                    if requested_time.cadence_policy is CadencePolicy.EXACT
                    else MatchCode.CADENCE_TOO_COARSE)
            checks.append(_check("cadence", cadence_ok, code,
                                 hint="temporal-resample"))
            anchor = offered_time.anchor or offered_time.start
            requested_anchor = requested_time.anchor or requested_time.start
            aligned = (anchor is not None and requested_anchor is not None
                       and _integer_multiple(
                           abs(_seconds_between(requested_anchor, anchor)),
                           offered_cadence))
            checks.append(_check("time_alignment", aligned,
                                 MatchCode.TIME_ALIGNMENT_MISMATCH,
                                 hint="temporal-resample"))
        reference_ok = (requested_time.reference_time is None
                        or offered_time.reference_time == requested_time.reference_time)
        checks.append(_check("reference_time", reference_ok,
                             MatchCode.TIME_REFERENCE_MISMATCH))
        if offered_time.max_gap_s is None:
            checks.append(_check("temporal_gaps", False,
                                 MatchCode.TEMPORAL_GAPS_UNKNOWN))
        else:
            checks.append(_check(
                "temporal_gaps",
                decimal_value(offered_time.max_gap_s)
                <= decimal_value(requested_time.maximum_gap_s),
                MatchCode.TEMPORAL_GAPS_EXCEED_POLICY, hint="gap-filling"))

    if requirement.vertical is not None:
        offered_vertical = descriptor.vertical_support
        checks.append(_check("vertical_present", offered_vertical is not None,
                             MatchCode.VERTICAL_REQUIRED_MISSING))
        if offered_vertical is not None:
            checks.append(_check("vertical_kind",
                                 offered_vertical.kind is requirement.vertical.kind,
                                 MatchCode.VERTICAL_KIND_MISMATCH,
                                 hint="vertical-transformation"))
            checks.append(_check("vertical_datum",
                                 offered_vertical.datum == requirement.vertical.datum,
                                 MatchCode.VERTICAL_DATUM_MISMATCH,
                                 hint="vertical-transformation"))
            checks.append(_check("vertical_units",
                                 offered_vertical.unit == requirement.vertical.unit,
                                 MatchCode.VERTICAL_UNITS_MISMATCH,
                                 hint="unit-conversion"))
            checks.append(_check(
                "vertical_levels",
                set(requirement.vertical.levels).issubset(offered_vertical.levels),
                MatchCode.VERTICAL_LEVEL_MISSING,
                hint="vertical-interpolation"))

    checks.append(_check("origin", descriptor.origin in requirement.allowed_origins,
                         MatchCode.ORIGIN_DISALLOWED))

    if requirement.max_native_resolution is not None:
        if descriptor.native_resolution is None:
            checks.append(_check("native_resolution", False,
                                 MatchCode.NATIVE_RESOLUTION_UNKNOWN))
        else:
            result = _scale_leq(descriptor.native_resolution,
                                requirement.max_native_resolution)
            checks.append(_check(
                "native_resolution", result is True,
                (MatchCode.NATIVE_RESOLUTION_INCOMPARABLE if result is None
                 else MatchCode.NATIVE_RESOLUTION_TOO_COARSE)))

    missing = descriptor.missingness
    if missing.status is MissingnessStatus.UNKNOWN:
        if requirement.missing_policy.unknown_policy.value == "ALLOW_WITH_CAVEAT":
            checks.append(CompatibilityCheck(
                "missingness", CheckStatus.UNKNOWN_ALLOWED,
                MatchCode.QUALITY_UNKNOWN_ALLOWED, "missingness is explicitly allowed"))
        else:
            checks.append(_check("missingness", False,
                                 MatchCode.MISSINGNESS_UNKNOWN))
    else:
        checks.append(_check(
            "missingness_fraction",
            decimal_value(missing.max_fraction or "0")
            <= decimal_value(requirement.missing_policy.max_fraction),
            MatchCode.MISSINGNESS_EXCEEDS_POLICY))
        accepted = requirement.missing_policy.accepted_encodings
        encodings_ok = not accepted or set(missing.encodings).issubset(accepted)
        checks.append(_check("missingness_encoding", encodings_ok,
                             MatchCode.MISSINGNESS_ENCODING_MISMATCH))

    evidence_needed = bool(
        requirement.minimum_evidence.required_profile_schema
        or requirement.minimum_evidence.required_metric_ids
        or requirement.max_effective_resolution)
    evidence_metrics = set(requirement.minimum_evidence.required_metric_ids)
    if requirement.max_effective_resolution:
        evidence_metrics.add(
            requirement.max_effective_resolution.metric_definition_id)

    if evidence_profile is None:
        if evidence_needed or not requirement.minimum_evidence.allow_unknown_empirical:
            checks.append(_check("evidence_profile", False,
                                 MatchCode.EVIDENCE_PROFILE_MISSING))
        else:
            checks.append(CompatibilityCheck(
                "evidence_profile", CheckStatus.UNKNOWN_ALLOWED,
                MatchCode.QUALITY_UNKNOWN_ALLOWED,
                "empirical quality is explicitly allowed to remain unknown"))
    else:
        if requirement.minimum_evidence.required_profile_schema:
            checks.append(_check(
                "evidence_schema",
                evidence_profile.schema_version
                == requirement.minimum_evidence.required_profile_schema,
                MatchCode.EVIDENCE_SCHEMA_MISMATCH))
        if evidence_metrics and evidence_snapshot is None:
            checks.append(_check("evidence_snapshot", False,
                                 MatchCode.EVIDENCE_SNAPSHOT_MISSING))
        elif evidence_snapshot is not None:
            checks.append(_check("evidence_snapshot",
                                 evidence_snapshot.contains(evidence_profile),
                                 MatchCode.EVIDENCE_SNAPSHOT_MISMATCH))
        if evidence_metrics and evidence_subject is None:
            checks.append(_check("evidence_subject", False,
                                 MatchCode.EVIDENCE_SUBJECT_MISSING))
        elif evidence_subject is not None:
            checks.append(_check("evidence_subject",
                                 evidence_profile.subject == evidence_subject,
                                 MatchCode.EVIDENCE_SUBJECT_MISMATCH))

        for metric_id in sorted(evidence_metrics):
            claim = evidence_profile.claim_for(metric_id)
            hard_bound = (requirement.max_effective_resolution is not None
                          and metric_id == requirement.max_effective_resolution.metric_definition_id)
            if (claim is not None
                    and claim.status is EvidenceStatus.NOT_APPLICABLE):
                checks.append(_check(
                    f"evidence:{metric_id}", False,
                    MatchCode.EVIDENCE_NOT_APPLICABLE))
                continue
            if claim is None or claim.status is EvidenceStatus.UNKNOWN:
                if (not hard_bound
                        and requirement.minimum_evidence.allow_unknown_empirical):
                    checks.append(CompatibilityCheck(
                        f"evidence:{metric_id}", CheckStatus.UNKNOWN_ALLOWED,
                        MatchCode.QUALITY_UNKNOWN_ALLOWED,
                        "metric is explicitly allowed to remain unknown"))
                else:
                    checks.append(_check(f"evidence:{metric_id}", False,
                                         MatchCode.EVIDENCE_CLAIM_UNKNOWN))
                continue
            checks.append(_check(
                f"evidence_applicability:{metric_id}",
                claim.applicability is not None and _applicability_contains(
                    claim.applicability, requirement, regimes),
                MatchCode.EVIDENCE_NOT_APPLICABLE))
            if evidence_snapshot is not None:
                definition = evidence_snapshot.evaluator.definition_for(metric_id)
                method_ok = (definition is not None
                             and claim.evaluator_id == evidence_snapshot.evaluator.evaluator_id
                             and claim.protocol_id == definition.method_id)
                checks.append(_check(f"evidence_method:{metric_id}", method_ok,
                                     MatchCode.EVIDENCE_METHOD_MISMATCH))
                checks.append(_check(
                    f"evidence_definition_units:{metric_id}",
                    definition is not None and claim.unit == definition.unit,
                    MatchCode.EVIDENCE_UNITS_MISMATCH))
            if hard_bound:
                bound = requirement.max_effective_resolution
                checks.append(_check(f"evidence_units:{metric_id}",
                                     claim.unit == bound.unit,
                                     MatchCode.EVIDENCE_UNITS_MISMATCH))
                kind_ok = (not bound.require_conservative_bound
                           or claim.bound_kind is BoundKind.CONSERVATIVE_UPPER)
                checks.append(_check(f"evidence_bound_kind:{metric_id}", kind_ok,
                                     MatchCode.EVIDENCE_BOUND_KIND_MISMATCH))
                value_ok = (claim.unit == bound.unit and claim.value is not None
                            and decimal_value(claim.value)
                            <= decimal_value(bound.maximum))
                checks.append(_check(f"evidence_bound:{metric_id}", value_ok,
                                     MatchCode.EVIDENCE_BOUND_FAILED))

    return CompatibilityProof(
        requirement.requirement_id,
        descriptor.descriptor_id,
        evidence_profile.profile_id if evidence_profile else None,
        evidence_snapshot.snapshot_id if evidence_snapshot else None,
        evidence_subject.identity if evidence_subject else None,
        tuple(checks),
    )
