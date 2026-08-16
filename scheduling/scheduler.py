"""Event-driven scheduling, and the layer runner it is meant to beat.

Three policies share one deterministic discrete-event simulator so their
makespans are comparable on identical inputs:

* ``LAYERED`` — the Stage-1 shape. Every task in a level must finish before any
  task in the next level starts. One slow task in a level stalls everything
  behind it, including work that never depended on it.
* ``FIFO`` — event-driven but blind: ready work runs in task-ID order, so a
  task on the critical path waits behind unrelated work that happens to sort
  first.
* ``EVENT_DRIVEN`` — ready work is ranked by remaining critical path with
  aging, and each task starts the moment its last dependency commits and its
  resources are free.

The simulator is honest about what it is: durations come from declared
estimates or measured history, not from running the science. It answers "does
this policy order work better", not "how long will this take on your node".
Resource feasibility inside it is real, though — every start goes through the
same :class:`ReservationLedger` that refuses to oversubscribe.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from capabilities.implementation import _required_text

from .observations import ObservationHistory
from .priority import (
    PriorityPolicy,
    ScheduledNode,
    critical_path_ranks,
    order_ready_tasks,
    topological_order,
)
from .resources import (
    ExecutionSite,
    ReservationLedger,
    ResourceEnvelopeSpec,
    best_fit_site,
)


class SchedulingPolicy(str, Enum):
    LAYERED = "LAYERED"
    FIFO = "FIFO"
    EVENT_DRIVEN = "EVENT_DRIVEN"


@dataclass(frozen=True)
class SchedulableTask:
    """One unit of work with its dependencies, envelope, and estimate."""

    task_key: str
    envelope: ResourceEnvelopeSpec
    declared_duration_s: float
    dependencies: tuple[str, ...] = ()
    required_environments: tuple[str, ...] = ()
    required_networks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.task_key, "task_key")
        if not isinstance(self.envelope, ResourceEnvelopeSpec):
            raise TypeError("task envelope is invalid")
        if not self.envelope.is_runnable_request:
            raise ValueError("a task needs at least one core and some memory")
        if (isinstance(self.declared_duration_s, bool)
                or not isinstance(self.declared_duration_s, (int, float))
                or not math.isfinite(float(self.declared_duration_s))
                or self.declared_duration_s < 0):
            raise ValueError(
                "declared duration must be a finite non-negative number")
        for values, label in ((self.dependencies, "dependencies"),
                              (self.required_environments, "environments"),
                              (self.required_networks, "networks")):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item
                           for item in values)):
                raise TypeError(f"task {label} must be a text tuple")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "envelope": self.envelope.to_dict(),
            "declared_duration_s": self.declared_duration_s,
            "dependencies": list(self.dependencies),
            "required_environments": list(self.required_environments),
            "required_networks": list(self.required_networks),
        }


@dataclass(frozen=True)
class ScheduledStart:
    task_key: str
    site_id: str
    start_s: float
    finish_s: float

    @property
    def duration_s(self) -> float:
        return self.finish_s - self.start_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key, "site_id": self.site_id,
            "start_s": round(self.start_s, 6),
            "finish_s": round(self.finish_s, 6),
        }


@dataclass(frozen=True)
class ScheduleResult:
    """The outcome of simulating one policy over one graph."""

    policy: SchedulingPolicy
    makespan_s: float
    starts: tuple[ScheduledStart, ...]
    max_wait_s: float
    peak_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    oversubscribed: bool = False

    def start_of(self, task_key: str) -> ScheduledStart:
        for item in self.starts:
            if item.task_key == task_key:
                return item
        raise KeyError(f"{task_key!r} was never scheduled")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.value,
            "makespan_s": round(self.makespan_s, 6),
            "task_count": len(self.starts),
            "max_wait_s": round(self.max_wait_s, 6),
            "peak_usage": self.peak_usage,
            "oversubscribed": self.oversubscribed,
        }


def _levels(tasks: Mapping[str, SchedulableTask]) -> dict[str, int]:
    """Depth from the sources, which is what a layer runner barriers on."""
    nodes = {key: ScheduledNode(key, task.declared_duration_s,
                                task.dependencies)
             for key, task in tasks.items()}
    level: dict[str, int] = {}
    for key in topological_order(nodes):
        parents = tasks[key].dependencies
        level[key] = 0 if not parents else 1 + max(
            level[parent] for parent in parents)
    return level


def simulate_schedule(
    tasks: Iterable[SchedulableTask],
    sites: Iterable[ExecutionSite],
    *,
    policy: SchedulingPolicy = SchedulingPolicy.EVENT_DRIVEN,
    priority_policy: PriorityPolicy = PriorityPolicy(),
    history: ObservationHistory | None = None,
) -> ScheduleResult:
    """Run one policy to completion over a frozen graph, deterministically."""
    by_key = {task.task_key: task for task in tasks}
    if not by_key:
        raise ValueError("nothing to schedule")
    site_values = tuple(sites)
    ledger = ReservationLedger(site_values)

    def duration(task: SchedulableTask) -> float:
        if history is None:
            return task.declared_duration_s
        return history.duration_estimate(
            task.task_key, task.declared_duration_s).value

    nodes = [ScheduledNode(key, duration(task), task.dependencies)
             for key, task in by_key.items()]
    ranks = critical_path_ranks(nodes)
    level = _levels(by_key) if policy is SchedulingPolicy.LAYERED else {}

    unmet = {key: len(task.dependencies) for key, task in by_key.items()}
    ready_at = {key: 0.0 for key, count in unmet.items() if count == 0}
    running: dict[str, tuple[float, str]] = {}     # key -> (finish, site)
    starts: list[ScheduledStart] = []
    peak: dict[str, dict[str, int]] = {
        site.site_id: {"cpu_cores": 0, "memory_mb": 0} for site in site_values}
    now = 0.0
    max_wait = 0.0
    oversubscribed = False
    completed: set[str] = set()
    current_level = 0

    while len(completed) < len(by_key):
        # --- start everything that can start at this instant --------------
        eligible = sorted(ready_at)
        if policy is SchedulingPolicy.LAYERED:
            # A barrier: only this level may run, and the next level cannot
            # begin until every task of this one has finished.
            eligible = [key for key in eligible if level[key] == current_level]
        if policy is SchedulingPolicy.EVENT_DRIVEN:
            waiting = {key: now - ready_at[key] for key in eligible}
            ordered = order_ready_tasks(eligible, ranks, waiting,
                                        priority_policy)
        else:
            ordered = tuple(eligible)          # FIFO / layered: by task ID

        for key in ordered:
            task = by_key[key]
            site_id = best_fit_site(
                ledger, task.envelope,
                required_environments=tuple(sorted(task.required_environments)),
                required_networks=tuple(sorted(task.required_networks)))
            if site_id is None:
                continue                        # no room yet; try again later
            ledger.reserve(key, site_id, task.envelope)
            if not ledger.invariant_holds():
                oversubscribed = True
            used = ledger.used(site_id)
            peak[site_id]["cpu_cores"] = max(
                peak[site_id]["cpu_cores"], used.cpu_cores)
            peak[site_id]["memory_mb"] = max(
                peak[site_id]["memory_mb"], used.memory_mb)
            max_wait = max(max_wait, now - ready_at[key])
            finish = now + duration(task)
            starts.append(ScheduledStart(key, site_id, now, finish))
            running[key] = (finish, site_id)
            del ready_at[key]

        if not running:
            raise RuntimeError(
                "scheduling deadlocked: ready work does not fit any site")

        # --- advance to the next completion -------------------------------
        now = min(finish for finish, _site in running.values())
        finished = sorted(key for key, (finish, _site) in running.items()
                          if finish <= now)
        for key in finished:
            del running[key]
            ledger.release(key)
            completed.add(key)
            for candidate, task in by_key.items():
                if key in task.dependencies:
                    unmet[candidate] -= 1
                    if unmet[candidate] == 0:
                        # Ready the instant its last dependency commits.
                        ready_at[candidate] = now
        if policy is SchedulingPolicy.LAYERED and not running and not any(
                level[key] == current_level for key in ready_at):
            current_level += 1

    return ScheduleResult(
        policy=policy, makespan_s=now, starts=tuple(starts),
        max_wait_s=max_wait, peak_usage=peak, oversubscribed=oversubscribed)


def compare_policies(
    tasks: Iterable[SchedulableTask],
    sites: Iterable[ExecutionSite],
    *,
    priority_policy: PriorityPolicy = PriorityPolicy(),
    history: ObservationHistory | None = None,
) -> dict[str, ScheduleResult]:
    """Run every policy over identical inputs for an apples-to-apples makespan."""
    task_values = tuple(tasks)
    site_values = tuple(sites)
    return {
        policy.value: simulate_schedule(
            task_values, site_values, policy=policy,
            priority_policy=priority_policy, history=history)
        for policy in SchedulingPolicy
    }


__all__ = [
    "SchedulableTask",
    "ScheduleResult",
    "ScheduledStart",
    "SchedulingPolicy",
    "compare_policies",
    "simulate_schedule",
]
