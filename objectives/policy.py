"""Named selection policies, and the refusal to fake a quality optimizer.

The MVP has exactly one automatic objective: minimum declared cost, subject to
hard admissibility constraints.  Empirical quality is *not* a second objective
it can optimize, because doing so honestly would require a metric that is
comparable across every alternative in the graph, and no such metric exists in
general.

So a quality request does not return a ranked answer.  It returns
``CHOICE_REQUIRED`` plus the admissible named alternatives and whatever
comparable evidence exists as decision support.  A human then makes a versioned
choice, and that choice — or an explicitly declared non-quality fallback — is
recorded into the identity of the plan that runs.

There is deliberately no weighted "balanced quality" score anywhere in this
package.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any

from capabilities.implementation import _digest, _required_text
from engine.runtime.identity import require_object_fields, strict_hash
from plans import ProducerKind
from resolution import ProducerSelectionRef


class SelectionObjective(str, Enum):
    """What the caller wants the selector to optimize.

    ``MINIMUM_COST`` is automatic.  ``EMPIRICAL_QUALITY`` is not: it always
    routes through a human decision in the MVP.
    """

    MINIMUM_COST = "MINIMUM_COST"
    EMPIRICAL_QUALITY = "EMPIRICAL_QUALITY"


class ObjectiveStatus(str, Enum):
    RESOLVED = "RESOLVED"
    CHOICE_REQUIRED = "CHOICE_REQUIRED"


@dataclass(frozen=True)
class ChoiceRecord:
    """One human decision, versioned and attributable.

    A choice is only meaningful against the exact decision-support report the
    chooser actually saw, so ``report_id`` is part of its identity.  Presenting
    different alternatives and reusing an old choice is not possible.
    """

    report_id: str
    chosen_producer: ProducerSelectionRef
    choice_version: str
    rationale: str

    def __post_init__(self) -> None:
        _digest(self.report_id, "choice report_id")
        if not isinstance(self.chosen_producer, ProducerSelectionRef):
            raise TypeError("a choice must name a typed ProducerSelectionRef")
        _required_text(self.choice_version, "choice_version")
        _required_text(self.rationale, "choice rationale")

    @property
    def decision_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage6-choice-record-v1",
            "report_id": self.report_id,
            "chosen_producer": self.chosen_producer.to_dict(),
            "choice_version": self.choice_version,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChoiceRecord":
        raw = require_object_fields(
            value,
            {"schema", "report_id", "chosen_producer", "choice_version",
             "rationale"},
            "ChoiceRecord")
        if raw.pop("schema") != "stage6-choice-record-v1":
            raise ValueError("ChoiceRecord schema is not stage6-choice-record-v1")
        producer = raw["chosen_producer"]
        raw["chosen_producer"] = ProducerSelectionRef(
            ProducerKind(producer["producer_kind"]), producer["producer_id"])
        return cls(**raw)


@dataclass(frozen=True)
class FallbackDecision:
    """An explicit decision to stop asking about quality and use a policy.

    This exists so that "we gave up on comparing quality" is a recorded,
    attributable act rather than an invisible default.
    """

    report_id: str
    fallback_objective: SelectionObjective
    rationale: str

    def __post_init__(self) -> None:
        _digest(self.report_id, "fallback report_id")
        if not isinstance(self.fallback_objective, SelectionObjective):
            raise TypeError("fallback objective must be typed")
        if self.fallback_objective is SelectionObjective.EMPIRICAL_QUALITY:
            raise ValueError(
                "the quality objective cannot be its own fallback")
        _required_text(self.rationale, "fallback rationale")

    @property
    def decision_id(self) -> str:
        return strict_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage6-fallback-decision-v1",
            "report_id": self.report_id,
            "fallback_objective": self.fallback_objective.value,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FallbackDecision":
        raw = require_object_fields(
            value, {"schema", "report_id", "fallback_objective", "rationale"},
            "FallbackDecision")
        if raw.pop("schema") != "stage6-fallback-decision-v1":
            raise ValueError(
                "FallbackDecision schema is not stage6-fallback-decision-v1")
        raw["fallback_objective"] = SelectionObjective(raw["fallback_objective"])
        return cls(**raw)


@dataclass(frozen=True)
class ObjectiveRequest:
    """A resolution request carrying its objective and any prior decision."""

    objective: SelectionObjective = SelectionObjective.MINIMUM_COST
    choice: ChoiceRecord | None = None
    fallback: FallbackDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.objective, SelectionObjective):
            raise TypeError("objective must be a SelectionObjective")
        if self.choice is not None and not isinstance(self.choice, ChoiceRecord):
            raise TypeError("choice must be a ChoiceRecord")
        if self.fallback is not None and not isinstance(
                self.fallback, FallbackDecision):
            raise TypeError("fallback must be a FallbackDecision")
        if self.choice is not None and self.fallback is not None:
            raise ValueError(
                "a request carries either an explicit choice or a declared "
                "fallback, never both")
        if (self.objective is SelectionObjective.MINIMUM_COST
                and (self.choice is not None or self.fallback is not None)):
            raise ValueError(
                "a minimum-cost request needs no quality decision; it was "
                "never blocked on one")

    @property
    def decision(self) -> ChoiceRecord | FallbackDecision | None:
        return self.choice if self.choice is not None else self.fallback

    @property
    def decision_id(self) -> str | None:
        decision = self.decision
        return None if decision is None else decision.decision_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective.value,
            "choice": self.choice.to_dict() if self.choice else None,
            "fallback": self.fallback.to_dict() if self.fallback else None,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObjectiveRequest":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ObjectiveRequest")
        raw["objective"] = SelectionObjective(raw["objective"])
        raw["choice"] = (ChoiceRecord.from_dict(raw["choice"])
                         if raw["choice"] is not None else None)
        raw["fallback"] = (FallbackDecision.from_dict(raw["fallback"])
                           if raw["fallback"] is not None else None)
        return cls(**raw)


__all__ = [
    "ChoiceRecord",
    "FallbackDecision",
    "ObjectiveRequest",
    "ObjectiveStatus",
    "SelectionObjective",
]
