"""When two alternatives' empirical metrics may honestly be compared.

Two numbers carrying the same unit are not automatically comparable.  A bias of
0.4 m/s measured against one reference network, by one evaluator version, over
one region, says nothing about a 0.3 m/s measured against a different reference
somewhere else.  Presenting those side by side as a ranking would be the single
most misleading thing this system could do, because it looks like science.

So comparability is decided explicitly and conservatively.  Every alternative
must carry a *known* claim for the same metric, produced by the same evaluator
and protocol against the same reference manifest, in the same unit and the same
bound kind, with applicability that actually covers the request.  Anything else
is reported as incomparable with a typed reason.

Even when the metrics are comparable, overlapping confidence intervals mean the
evidence did not separate the alternatives, and the report says so rather than
letting a smaller point estimate imply a winner.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from capabilities.implementation import _required_text
from contracts import (
    BoundKind,
    EvidenceProfile,
    EvidenceStatus,
    Requirement,
    UncertaintyStatus,
)
from contracts.identity import decimal_value
# One source of truth for applicability: the same predicate ``direct_match``
# uses to admit a producer at all.  A divergent copy here could report a metric
# as applicable that the matcher rejected.
from contracts.matching import _applicability_contains
from engine.runtime.identity import require_object_fields, strict_hash


class ComparabilityCode(str, Enum):
    SINGLE_ALTERNATIVE = "SINGLE_ALTERNATIVE"
    NO_SHARED_METRIC = "NO_SHARED_METRIC"
    METRIC_NOT_KNOWN = "METRIC_NOT_KNOWN"
    REFERENCE_MANIFEST_DIFFERS = "REFERENCE_MANIFEST_DIFFERS"
    EVALUATOR_DIFFERS = "EVALUATOR_DIFFERS"
    PROTOCOL_DIFFERS = "PROTOCOL_DIFFERS"
    UNIT_DIFFERS = "UNIT_DIFFERS"
    BOUND_KIND_DIFFERS = "BOUND_KIND_DIFFERS"
    APPLICABILITY_DOES_NOT_COVER_REQUEST = (
        "APPLICABILITY_DOES_NOT_COVER_REQUEST")


@dataclass(frozen=True)
class MetricReading:
    """One alternative's claim for one metric, as presented to a human."""

    producer_id: str
    metric_definition_id: str
    status: EvidenceStatus
    unit: str
    value: str | None = None
    bound_kind: BoundKind | None = None
    reference_manifest_id: str | None = None
    evaluator_id: str | None = None
    protocol_id: str | None = None
    applicability_covers_request: bool = False
    uncertainty_status: UncertaintyStatus | None = None
    uncertainty_lower: str | None = None
    uncertainty_upper: str | None = None
    uncertainty_confidence_level: str | None = None
    uncertainty_method_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.producer_id, "reading producer_id")
        _required_text(self.metric_definition_id, "reading metric id")
        if not isinstance(self.status, EvidenceStatus):
            raise TypeError("reading status must be typed")
        if type(self.applicability_covers_request) is not bool:
            raise TypeError("applicability coverage must be bool")

    @property
    def known(self) -> bool:
        return self.status is EvidenceStatus.KNOWN

    @property
    def interval(self) -> tuple[Decimal, Decimal] | None:
        if (self.uncertainty_status is not UncertaintyStatus.KNOWN
                or self.uncertainty_lower is None
                or self.uncertainty_upper is None):
            return None
        return (decimal_value(self.uncertainty_lower),
                decimal_value(self.uncertainty_upper))

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["status"] = self.status.value
        payload["bound_kind"] = (
            self.bound_kind.value if self.bound_kind else None)
        payload["uncertainty_status"] = (
            self.uncertainty_status.value if self.uncertainty_status else None)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MetricReading":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "MetricReading")
        raw["status"] = EvidenceStatus(raw["status"])
        if raw["bound_kind"] is not None:
            raw["bound_kind"] = BoundKind(raw["bound_kind"])
        if raw["uncertainty_status"] is not None:
            raw["uncertainty_status"] = UncertaintyStatus(
                raw["uncertainty_status"])
        return cls(**raw)


def read_metric(producer_id: str, profile: EvidenceProfile | None,
                metric_definition_id: str,
                requirement: Requirement) -> MetricReading:
    """Extract one alternative's metric claim without interpreting it."""
    if profile is None:
        return MetricReading(
            producer_id=producer_id,
            metric_definition_id=metric_definition_id,
            status=EvidenceStatus.UNKNOWN, unit="1",
            reason="this producer declares no evidence profile")
    claim = profile.claim_for(metric_definition_id)
    if claim is None:
        return MetricReading(
            producer_id=producer_id,
            metric_definition_id=metric_definition_id,
            status=EvidenceStatus.UNKNOWN, unit="1",
            reason="the evidence profile carries no claim for this metric")
    covers = (claim.applicability is not None and _applicability_contains(
        claim.applicability, requirement, requirement.required_regimes))
    uncertainty = claim.estimate_uncertainty
    return MetricReading(
        producer_id=producer_id,
        metric_definition_id=metric_definition_id,
        status=claim.status,
        unit=claim.unit,
        value=claim.value,
        bound_kind=claim.bound_kind,
        reference_manifest_id=claim.reference_manifest_id,
        evaluator_id=claim.evaluator_id,
        protocol_id=claim.protocol_id,
        applicability_covers_request=covers,
        uncertainty_status=uncertainty.status,
        uncertainty_lower=uncertainty.lower_bound,
        uncertainty_upper=uncertainty.upper_bound,
        uncertainty_confidence_level=uncertainty.confidence_level,
        uncertainty_method_id=uncertainty.method_id,
        reason=claim.reason,
    )


@dataclass(frozen=True)
class ComparabilityVerdict:
    """Whether these readings may be compared, and what that comparison shows."""

    comparable: bool
    metric_definition_id: str
    blocking_codes: tuple[ComparabilityCode, ...]
    detail: str
    shared_reference_manifest_id: str | None = None
    shared_evaluator_id: str | None = None
    intervals_overlap: bool | None = None

    def __post_init__(self) -> None:
        _required_text(self.metric_definition_id, "verdict metric id")
        if (not isinstance(self.blocking_codes, tuple)
                or not all(isinstance(item, ComparabilityCode)
                           for item in self.blocking_codes)):
            raise TypeError("blocking codes must be typed")
        if self.comparable != (not self.blocking_codes):
            raise ValueError(
                "comparability must agree with the blocking codes")
        if not self.comparable and not self.detail:
            raise ValueError("an incomparable verdict must explain itself")
        if self.comparable and self.intervals_overlap is None:
            raise ValueError(
                "a comparable verdict must report interval separation")

    @property
    def separation_established(self) -> bool:
        """True only when comparison is valid *and* the intervals separate.

        This is the flag a caller should read before saying one alternative is
        empirically better.  It is never true on incomparable evidence, and
        never true when the confidence intervals overlap.
        """
        return self.comparable and self.intervals_overlap is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "metric_definition_id": self.metric_definition_id,
            "blocking_codes": [item.value for item in self.blocking_codes],
            "detail": self.detail,
            "shared_reference_manifest_id": self.shared_reference_manifest_id,
            "shared_evaluator_id": self.shared_evaluator_id,
            "intervals_overlap": self.intervals_overlap,
            "separation_established": self.separation_established,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ComparabilityVerdict":
        raw = require_object_fields(
            value,
            {"comparable", "metric_definition_id", "blocking_codes", "detail",
             "shared_reference_manifest_id", "shared_evaluator_id",
             "intervals_overlap", "separation_established"},
            "ComparabilityVerdict")
        raw.pop("separation_established")
        raw["blocking_codes"] = tuple(
            ComparabilityCode(item) for item in raw["blocking_codes"])
        return cls(**raw)

    @property
    def verdict_id(self) -> str:
        return strict_hash(self.to_dict())


def assess_comparability(readings: Iterable[MetricReading],
                         metric_definition_id: str) -> ComparabilityVerdict:
    """Decide, conservatively, whether these readings can be compared.

    Every blocking condition found is reported, not just the first, so a
    scientist can see everything standing between them and a valid comparison.
    """
    values = tuple(readings)
    codes: list[ComparabilityCode] = []
    details: list[str] = []

    if len(values) < 2:
        return ComparabilityVerdict(
            False, metric_definition_id,
            (ComparabilityCode.SINGLE_ALTERNATIVE,),
            "fewer than two alternatives carry this metric, so there is "
            "nothing to compare")
    if any(item.metric_definition_id != metric_definition_id
           for item in values):
        return ComparabilityVerdict(
            False, metric_definition_id,
            (ComparabilityCode.NO_SHARED_METRIC,),
            "the readings do not all describe the same metric definition")

    unknown = [item.producer_id for item in values if not item.known]
    if unknown:
        codes.append(ComparabilityCode.METRIC_NOT_KNOWN)
        details.append(
            f"no known claim for {sorted(unknown)}")

    inapplicable = [item.producer_id for item in values
                    if item.known and not item.applicability_covers_request]
    if inapplicable:
        codes.append(ComparabilityCode.APPLICABILITY_DOES_NOT_COVER_REQUEST)
        details.append(
            f"evidence does not apply to this request for {sorted(inapplicable)}")

    known = [item for item in values if item.known]
    for attribute, code, label in (
            ("reference_manifest_id",
             ComparabilityCode.REFERENCE_MANIFEST_DIFFERS, "reference manifest"),
            ("evaluator_id", ComparabilityCode.EVALUATOR_DIFFERS, "evaluator"),
            ("protocol_id", ComparabilityCode.PROTOCOL_DIFFERS, "protocol"),
            ("unit", ComparabilityCode.UNIT_DIFFERS, "unit"),
            ("bound_kind", ComparabilityCode.BOUND_KIND_DIFFERS, "bound kind")):
        distinct = {getattr(item, attribute) for item in known}
        if len(distinct) > 1:
            codes.append(code)
            details.append(f"alternatives were evaluated with a different {label}")

    if codes:
        return ComparabilityVerdict(
            False, metric_definition_id, tuple(sorted(set(codes),
                                                      key=lambda c: c.value)),
            "; ".join(details))

    intervals = [item.interval for item in known]
    if any(item is None for item in intervals):
        # Comparable in provenance, but without intervals we cannot claim the
        # difference is resolved.  Treat that as "overlapping" — the safe side.
        overlap = True
    else:
        lowest_upper = min(item[1] for item in intervals)  # type: ignore[index]
        highest_lower = max(item[0] for item in intervals)  # type: ignore[index]
        overlap = highest_lower <= lowest_upper
    return ComparabilityVerdict(
        True, metric_definition_id, (),
        "", known[0].reference_manifest_id, known[0].evaluator_id, overlap)


def readings_by_producer(
        readings: Iterable[MetricReading]) -> Mapping[str, MetricReading]:
    return {item.producer_id: item for item in readings}


__all__ = [
    "ComparabilityCode",
    "ComparabilityVerdict",
    "MetricReading",
    "assess_comparability",
    "read_metric",
    "readings_by_producer",
]
