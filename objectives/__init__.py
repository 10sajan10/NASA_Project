"""Stage-6 named selection policies and the human-owned quality decision.

The MVP optimizes exactly one thing automatically: minimum declared cost under
hard admissibility constraints.  Empirical quality is never optimized silently.
A quality request returns ``CHOICE_REQUIRED`` with real, globally consistent
alternatives and an explicit verdict on whether their evidence can honestly be
compared at all; a human's versioned choice then drives a constrained re-solve
and is bound into the resulting plan's identity.
"""

from .alternatives import (
    SourceAlternative,
    admissible_alternatives,
    candidate_producers_for_concept,
    enumerate_source_alternatives,
)
from .comparability import (
    ComparabilityCode,
    ComparabilityVerdict,
    MetricReading,
    assess_comparability,
    read_metric,
)
from .decision import (
    DECISION_SNAPSHOT_NAME,
    ChoiceRequiredReport,
    ObjectiveOutcome,
    StaleChoiceError,
    build_choice_report,
    resolve_with_objective,
)
from .policy import (
    ChoiceRecord,
    FallbackDecision,
    ObjectiveRequest,
    ObjectiveStatus,
    SelectionObjective,
)

__all__ = [
    "DECISION_SNAPSHOT_NAME",
    "ChoiceRecord",
    "ChoiceRequiredReport",
    "ComparabilityCode",
    "ComparabilityVerdict",
    "FallbackDecision",
    "MetricReading",
    "ObjectiveOutcome",
    "ObjectiveRequest",
    "ObjectiveStatus",
    "SelectionObjective",
    "SourceAlternative",
    "StaleChoiceError",
    "admissible_alternatives",
    "assess_comparability",
    "build_choice_report",
    "candidate_producers_for_concept",
    "enumerate_source_alternatives",
    "read_metric",
    "resolve_with_objective",
]
