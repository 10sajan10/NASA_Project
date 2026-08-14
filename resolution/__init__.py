"""Stage-3 recursive workflow resolution and exact global selection."""

from .benchmark import PlanningBenchmarkProfile

from .hypergraph import (
    ArtifactAvailabilitySnapshot,
    ArtifactCommitRecord,
    ArtifactCommitStatus,
    BackReference,
    BackReferenceKind,
    DiscoveryLimitCode,
    DiscoveryLimitReason,
    DiscoveryLimits,
    DiscoveryRejection,
    DiscoveryTimings,
    FeasibleDerivationHypergraph,
    FeasibleHypergraph,
    HypergraphBuilder,
    build_feasible_hypergraph,
)
from .milp import (
    DeploymentOption,
    MilpSelectionProblem,
    MilpSelectionResult,
    MilpSolveOptions,
    MilpStatus,
    ProducerSelectionRef,
    SelectionConstraints,
    solve_milp,
)
from .projection import project_oracle_problem
from .service import (
    PlanningMetrics,
    ResolutionOutcome,
    ResolutionStatus,
    WorkflowResolver,
)
from .validator import (
    ArtifactCommitAttestation,
    ProofReplayContext,
    SelectedPlanValidationReport,
    ValidationBlocker,
    ValidationBlockerTree,
    ValidationCode,
    validate_selected_plan,
)

__all__ = [
    "ArtifactAvailabilitySnapshot",
    "ArtifactCommitAttestation",
    "ArtifactCommitRecord",
    "ArtifactCommitStatus",
    "BackReference",
    "BackReferenceKind",
    "DeploymentOption",
    "DiscoveryLimitCode",
    "DiscoveryLimitReason",
    "DiscoveryLimits",
    "DiscoveryRejection",
    "DiscoveryTimings",
    "FeasibleDerivationHypergraph",
    "FeasibleHypergraph",
    "HypergraphBuilder",
    "MilpSelectionProblem",
    "MilpSelectionResult",
    "MilpSolveOptions",
    "MilpStatus",
    "PlanningMetrics",
    "PlanningBenchmarkProfile",
    "ProducerSelectionRef",
    "ProofReplayContext",
    "ResolutionOutcome",
    "ResolutionStatus",
    "SelectedPlanValidationReport",
    "SelectionConstraints",
    "ValidationBlocker",
    "ValidationBlockerTree",
    "ValidationCode",
    "WorkflowResolver",
    "build_feasible_hypergraph",
    "project_oracle_problem",
    "solve_milp",
    "validate_selected_plan",
]
