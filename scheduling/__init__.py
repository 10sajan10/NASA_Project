"""Stage-8 resource-aware local scheduling policy.

Critical-path plus aging priority, real CPU/memory/GPU/scratch reservations
that refuse to oversubscribe, best-fit placement with environment affinity,
nested-thread capping, and measured history that replaces declared estimates
only once there is enough of it.
"""

from .observations import (
    DeploymentRevision,
    Estimate,
    EstimateSource,
    ObservationHistory,
    TaskObservation,
)
from .priority import (
    PriorityPolicy,
    ScheduledNode,
    critical_path_ranks,
    order_ready_tasks,
    topological_order,
)
from .resources import (
    THREAD_ENVIRONMENT_VARIABLES,
    ExecutionSite,
    OversubscriptionError,
    Reservation,
    ReservationLedger,
    ResourceEnvelopeSpec,
    best_fit_site,
    detect_capacity,
    thread_environment,
)
from .scheduler import (
    SchedulableTask,
    ScheduledStart,
    ScheduleResult,
    SchedulingPolicy,
    compare_policies,
    simulate_schedule,
)

__all__ = [
    "THREAD_ENVIRONMENT_VARIABLES",
    "DeploymentRevision",
    "Estimate",
    "EstimateSource",
    "ExecutionSite",
    "ObservationHistory",
    "OversubscriptionError",
    "PriorityPolicy",
    "Reservation",
    "ReservationLedger",
    "ResourceEnvelopeSpec",
    "SchedulableTask",
    "ScheduleResult",
    "ScheduledNode",
    "ScheduledStart",
    "SchedulingPolicy",
    "TaskObservation",
    "best_fit_site",
    "compare_policies",
    "critical_path_ranks",
    "detect_capacity",
    "order_ready_tasks",
    "simulate_schedule",
    "thread_environment",
    "topological_order",
]
