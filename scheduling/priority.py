"""Ranking work by critical path, with aging so nothing starves.

Two forces, deliberately in tension:

* **Critical path** — the longest remaining chain of work below a task. Running
  a task that unblocks a long tail first is what shortens makespan; FIFO cannot
  see that, which is why an imbalanced graph runs badly under it.
* **Aging** — the longer a ready task waits, the more its priority rises. Pure
  critical-path ranking will starve a short off-path branch forever if new
  high-rank work keeps arriving, and a scheduler that never runs your small job
  is broken no matter how good its makespan looks.

Rank is computed on *logical/template* nodes; Stage-7 partitions inherit their
template's rank rather than each computing their own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from capabilities.implementation import _required_text


@dataclass(frozen=True)
class ScheduledNode:
    """One logical unit of work as the scheduler sees it."""

    task_key: str
    duration_estimate_s: float
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.task_key, "scheduled node task_key")
        if (isinstance(self.duration_estimate_s, bool)
                or not isinstance(self.duration_estimate_s, (int, float))
                or self.duration_estimate_s < 0):
            raise ValueError("duration estimate must be non-negative")
        if (not isinstance(self.dependencies, tuple)
                or any(not isinstance(item, str) or not item
                       for item in self.dependencies)):
            raise TypeError("dependencies must be a text tuple")
        if self.task_key in self.dependencies:
            raise ValueError(f"task {self.task_key!r} depends on itself")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "duration_estimate_s": self.duration_estimate_s,
            "dependencies": list(self.dependencies),
        }


def _dependents(nodes: Mapping[str, ScheduledNode]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {key: [] for key in nodes}
    for node in nodes.values():
        for parent in node.dependencies:
            if parent not in nodes:
                raise KeyError(
                    f"task {node.task_key!r} depends on unknown {parent!r}")
            result[parent].append(node.task_key)
    return result


def topological_order(nodes: Mapping[str, ScheduledNode]) -> tuple[str, ...]:
    """Deterministic topological order, refusing cycles."""
    dependents = _dependents(nodes)
    remaining = {key: len(node.dependencies) for key, node in nodes.items()}
    ready = sorted(key for key, count in remaining.items() if count == 0)
    order: list[str] = []
    while ready:
        key = ready.pop(0)
        order.append(key)
        for child in sorted(dependents[key]):
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
        ready.sort()
    if len(order) != len(nodes):
        raise ValueError("the scheduling graph contains a cycle")
    return tuple(order)


def critical_path_ranks(nodes: Iterable[ScheduledNode]) -> dict[str, float]:
    """Longest remaining path from each node to a sink, including itself.

    Computed once over the whole graph in reverse topological order, so it is
    O(V+E) rather than a search per task.
    """
    by_key = {node.task_key: node for node in nodes}
    if not by_key:
        return {}
    dependents = _dependents(by_key)
    ranks: dict[str, float] = {}
    for key in reversed(topological_order(by_key)):
        downstream = max(
            (ranks[child] for child in dependents[key]), default=0.0)
        ranks[key] = by_key[key].duration_estimate_s + downstream
    return ranks


@dataclass(frozen=True)
class PriorityPolicy:
    """How critical path and waiting time combine into one ordering."""

    aging_weight_per_s: float = 1.0
    starvation_ceiling_s: float = 300.0

    def __post_init__(self) -> None:
        for name in ("aging_weight_per_s", "starvation_ceiling_s"):
            value = getattr(self, name)
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float)) or value < 0):
                raise ValueError(f"{name} must be non-negative")

    def score(self, critical_path_rank: float, waiting_s: float) -> float:
        """Higher wins. Aging is capped so it cannot invert the graph forever.

        The ceiling matters: without it a long-waiting trivial task eventually
        outranks everything, and the schedule degenerates into FIFO with extra
        steps.
        """
        if waiting_s < 0:
            raise ValueError("waiting time cannot be negative")
        aged = min(waiting_s, self.starvation_ceiling_s) * self.aging_weight_per_s
        return critical_path_rank + aged


def order_ready_tasks(ready: Iterable[str], ranks: Mapping[str, float],
                      waiting: Mapping[str, float],
                      policy: PriorityPolicy) -> tuple[str, ...]:
    """Order ready work best-first, breaking ties deterministically by key."""
    scored = [
        (-policy.score(ranks.get(key, 0.0), waiting.get(key, 0.0)), key)
        for key in ready
    ]
    return tuple(key for _score, key in sorted(scored))


__all__ = [
    "PriorityPolicy",
    "ScheduledNode",
    "critical_path_ranks",
    "order_ready_tasks",
    "topological_order",
]
