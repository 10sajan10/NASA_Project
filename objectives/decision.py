"""The quality-request path: refuse to rank, present, then honour a choice.

``resolve_with_objective`` is the whole Stage-6 policy surface:

* a ``MINIMUM_COST`` request resolves automatically, exactly as before;
* an ``EMPIRICAL_QUALITY`` request *always* returns ``CHOICE_REQUIRED`` with the
  admissible alternatives and an explicit comparability verdict; and
* a request carrying a human's versioned choice, or a declared non-quality
  fallback, re-solves under that decision and binds the decision into the
  resulting plan's identity.

A choice is checked against the exact report it was made from.  If the
alternatives have changed since, the choice is refused and a fresh report is
returned rather than quietly applying a decision to a different world.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from capabilities.implementation import _digest, _required_text
from contracts import EvidenceProfile, EvidenceSnapshot, RequirementUse
from engine.runtime.identity import require_object_fields, strict_hash
from plans import PlanSnapshotRef
from resolution import (
    ResolutionOutcome,
    SelectionConstraints,
)

from .alternatives import (
    SourceAlternative,
    admissible_alternatives,
    enumerate_source_alternatives,
)
from .comparability import ComparabilityVerdict, assess_comparability
from .policy import (
    ChoiceRecord,
    FallbackDecision,
    ObjectiveRequest,
    ObjectiveStatus,
    SelectionObjective,
)

DECISION_SNAPSHOT_NAME = "objective_decision"


@dataclass(frozen=True)
class ChoiceRequiredReport:
    """Decision support for a human. Explicitly not a ranking."""

    report_id: str
    concept_id: str
    metric_definition_id: str
    alternatives: tuple[SourceAlternative, ...]
    comparability: ComparabilityVerdict
    evidence_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _digest(self.report_id, "choice report_id")
        _required_text(self.concept_id, "report concept_id")
        _required_text(self.metric_definition_id, "report metric id")
        if (not isinstance(self.alternatives, tuple)
                or not all(isinstance(item, SourceAlternative)
                           for item in self.alternatives)):
            raise TypeError("report alternatives are invalid")
        if not isinstance(self.comparability, ComparabilityVerdict):
            raise TypeError("report comparability verdict is invalid")
        if self.report_id != self.expected_id():
            raise ValueError("choice report identity does not verify")

    # These two are constants, not fields, so no caller can construct a report
    # that claims a ranking or a non-dominance result the MVP cannot support.
    @property
    def ranking_complete(self) -> bool:
        return False

    @property
    def nondominance_claimed(self) -> bool:
        return False

    @property
    def admissible(self) -> tuple[SourceAlternative, ...]:
        return admissible_alternatives(self.alternatives)

    def alternative_for(self, producer_id: str) -> SourceAlternative | None:
        return next((item for item in self.alternatives
                     if item.producer.producer_id == producer_id), None)

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.concept_id, self.metric_definition_id, self.alternatives,
            self.comparability, self.evidence_snapshot_id))

    @staticmethod
    def _payload(concept_id: str, metric_definition_id: str,
                 alternatives: tuple[SourceAlternative, ...],
                 comparability: ComparabilityVerdict,
                 evidence_snapshot_id: str | None) -> dict[str, Any]:
        return {
            "schema": "stage6-choice-required-report-v2",
            "concept_id": concept_id,
            "metric_definition_id": metric_definition_id,
            "alternatives": [item.to_dict() for item in alternatives],
            "comparability": comparability.to_dict(),
            # A choice is only meaningful against the evidence it was shown.
            # Binding the snapshot here means a report built from different
            # evidence is a different report, and a choice made against the
            # old one is refused as stale rather than silently reapplied.
            "evidence_snapshot_id": evidence_snapshot_id,
            "ranking_complete": False,
            "nondominance_claimed": False,
        }

    @classmethod
    def bind(cls, *, concept_id: str, metric_definition_id: str,
             alternatives: tuple[SourceAlternative, ...],
             comparability: ComparabilityVerdict,
             evidence_snapshot_id: str | None = None
             ) -> "ChoiceRequiredReport":
        return cls(
            strict_hash(cls._payload(
                concept_id, metric_definition_id, alternatives, comparability,
                evidence_snapshot_id)),
            concept_id, metric_definition_id, alternatives, comparability,
            evidence_snapshot_id)

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.concept_id, self.metric_definition_id, self.alternatives,
            self.comparability, self.evidence_snapshot_id)
        payload["report_id"] = self.report_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChoiceRequiredReport":
        raw = require_object_fields(
            value,
            {"schema", "report_id", "concept_id", "metric_definition_id",
             "alternatives", "comparability", "evidence_snapshot_id",
             "ranking_complete", "nondominance_claimed"},
            "ChoiceRequiredReport")
        if raw.pop("schema") != "stage6-choice-required-report-v2":
            raise ValueError(
                "ChoiceRequiredReport schema is not "
                "stage6-choice-required-report-v2")
        if raw.pop("ranking_complete") or raw.pop("nondominance_claimed"):
            raise ValueError(
                "an MVP report cannot claim a complete ranking or non-dominance")
        raw["alternatives"] = tuple(
            SourceAlternative.from_dict(item) for item in raw["alternatives"])
        raw["comparability"] = ComparabilityVerdict.from_dict(
            raw["comparability"])
        return cls(**raw)


class StaleChoiceError(RuntimeError):
    """A choice was made against a report that no longer describes the world."""

    def __init__(self, supplied: str, current: str) -> None:
        super().__init__(
            f"the supplied choice was made against report {supplied[:12]} but "
            f"the current alternatives are {current[:12]}; re-present the "
            "decision rather than applying a stale one")
        self.supplied = supplied
        self.current = current


@dataclass(frozen=True)
class ObjectiveOutcome:
    """Either a resolved plan, or an explicit request for a human decision."""

    status: ObjectiveStatus
    objective: SelectionObjective
    resolution: ResolutionOutcome | None
    report: ChoiceRequiredReport | None
    decision_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ObjectiveStatus):
            raise TypeError("objective status must be typed")
        if self.status is ObjectiveStatus.CHOICE_REQUIRED:
            if self.report is None or self.resolution is not None:
                raise ValueError(
                    "CHOICE_REQUIRED carries a report and no auto-selected plan")
        elif self.resolution is None:
            raise ValueError("a resolved outcome must carry its resolution")

    @property
    def choice_required(self) -> bool:
        return self.status is ObjectiveStatus.CHOICE_REQUIRED

    @property
    def snapshot_ref(self) -> PlanSnapshotRef | None:
        """The reference that binds this decision into the bound plan's ID."""
        if self.decision_id is None:
            return None
        return PlanSnapshotRef(DECISION_SNAPSHOT_NAME, self.decision_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "objective": self.objective.value,
            "decision_id": self.decision_id,
            "report": self.report.to_dict() if self.report else None,
            "resolution_status": (
                self.resolution.status.value if self.resolution else None),
            "selected_plan_id": (
                self.resolution.selection.plan.plan_id
                if self.resolution is not None
                and self.resolution.selection.plan is not None else None),
        }


def build_choice_report(
    baseline: ResolutionOutcome,
    resolve: Callable[[SelectionConstraints], ResolutionOutcome],
    *,
    concept_id: str,
    requirement_use: RequirementUse,
    metric_definition_id: str,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> ChoiceRequiredReport:
    """Enumerate real alternatives and judge whether they can be compared."""
    alternatives = enumerate_source_alternatives(
        baseline, resolve, concept_id=concept_id,
        requirement_use=requirement_use,
        metric_definition_id=metric_definition_id,
        evidence_snapshot=evidence_snapshot)
    readings = [item.reading for item in admissible_alternatives(alternatives)
                if item.reading is not None]
    comparability = assess_comparability(readings, metric_definition_id)
    return ChoiceRequiredReport.bind(
        concept_id=concept_id, metric_definition_id=metric_definition_id,
        alternatives=alternatives, comparability=comparability,
        evidence_snapshot_id=(
            evidence_snapshot.snapshot_id
            if evidence_snapshot is not None else None))


def resolve_with_objective(
    resolve: Callable[[SelectionConstraints], ResolutionOutcome],
    request: ObjectiveRequest,
    *,
    concept_id: str,
    requirement_use: RequirementUse,
    metric_definition_id: str,
    evidence_snapshot: EvidenceSnapshot | None = None,
) -> ObjectiveOutcome:
    """Apply a named selection policy to one planning request."""
    if not isinstance(request, ObjectiveRequest):
        raise TypeError("request must be an ObjectiveRequest")

    if request.objective is SelectionObjective.MINIMUM_COST:
        return ObjectiveOutcome(
            ObjectiveStatus.RESOLVED, SelectionObjective.MINIMUM_COST,
            resolve(SelectionConstraints()), None, None)

    baseline = resolve(SelectionConstraints())
    report = build_choice_report(
        baseline, resolve, concept_id=concept_id,
        requirement_use=requirement_use,
        metric_definition_id=metric_definition_id,
        evidence_snapshot=evidence_snapshot)

    decision = request.decision
    if decision is None:
        # The MVP never auto-selects on quality, even when the evidence happens
        # to be comparable and separated.  A human owns this call.
        return ObjectiveOutcome(
            ObjectiveStatus.CHOICE_REQUIRED, SelectionObjective.EMPIRICAL_QUALITY,
            None, report, None)

    if decision.report_id != report.report_id:
        raise StaleChoiceError(decision.report_id, report.report_id)

    if isinstance(decision, ChoiceRecord):
        chosen = report.alternative_for(decision.chosen_producer.producer_id)
        if chosen is None:
            raise ValueError(
                "the chosen producer is not among the presented alternatives")
        if not chosen.admissible:
            raise ValueError(
                "the chosen producer has no globally consistent plan: "
                f"{chosen.detail}")
        resolution = resolve(
            SelectionConstraints.bind(include=(decision.chosen_producer,)))
    elif isinstance(decision, FallbackDecision):
        resolution = resolve(SelectionConstraints())
    else:  # pragma: no cover - guarded by ObjectiveRequest construction
        raise TypeError("unsupported decision type")

    return ObjectiveOutcome(
        ObjectiveStatus.RESOLVED, SelectionObjective.EMPIRICAL_QUALITY,
        resolution, report, decision.decision_id)


__all__ = [
    "DECISION_SNAPSHOT_NAME",
    "ChoiceRequiredReport",
    "ObjectiveOutcome",
    "StaleChoiceError",
    "build_choice_report",
    "resolve_with_objective",
]
