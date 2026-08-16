"""Domain-neutral Stage-8 scheduling fixtures.

The imbalanced branch graph is built so that *ordering actually matters*. Six
short, unrelated tasks sort before the long critical chain by task ID, so a
blind FIFO policy grabs every core with work that unblocks nothing while the
chain — the thing that determines when the run can possibly finish — waits.

That is not a contrived trap; it is the ordinary shape of a scientific
workflow, where a long serial model run sits alongside many quick auxiliary
steps. A scheduler that cannot see the difference wastes the whole difference.
"""
from __future__ import annotations

from dataclasses import dataclass

from scheduling import (
    ExecutionSite,
    ResourceEnvelopeSpec,
    SchedulableTask,
)

SHORT_COUNT = 6
SHORT_DURATION_S = 3.0
CHAIN_LENGTH = 3
CHAIN_DURATION_S = 10.0
NODE_CORES = 2
NODE_MEMORY_MB = 4096

# The critical path is the floor no policy can beat: three 10 s links.
CRITICAL_PATH_S = CHAIN_LENGTH * CHAIN_DURATION_S


@dataclass(frozen=True)
class Stage8Fixture:
    tasks: tuple[SchedulableTask, ...]
    sites: tuple[ExecutionSite, ...]
    critical_path_s: float
    chain_keys: tuple[str, ...]


def make_imbalanced_graph(*, cores: int = NODE_CORES) -> Stage8Fixture:
    """Short unrelated work that sorts first, plus a long critical chain."""
    tasks: list[SchedulableTask] = [
        SchedulableTask(f"a-short-{index}",
                        ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256),
                        SHORT_DURATION_S)
        for index in range(SHORT_COUNT)
    ]
    chain_keys: list[str] = []
    for link in range(1, CHAIN_LENGTH + 1):
        key = f"z-chain-{link}"
        tasks.append(SchedulableTask(
            key, ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256),
            CHAIN_DURATION_S,
            dependencies=() if link == 1 else (f"z-chain-{link - 1}",)))
        chain_keys.append(key)
    site = ExecutionSite(
        "private-node", ResourceEnvelopeSpec(
            cpu_cores=cores, memory_mb=NODE_MEMORY_MB),
        environment_classes=(), network_classes=("none",))
    return Stage8Fixture(tuple(tasks), (site,), CRITICAL_PATH_S,
                         tuple(chain_keys))


def make_starvation_graph() -> Stage8Fixture:
    """One long high-rank chain and one tiny task nothing depends on.

    Without aging the tiny task always loses the ranking and runs last. It is
    the case that separates "good makespan" from "good scheduler".
    """
    tasks: list[SchedulableTask] = []
    for link in range(1, 5):
        tasks.append(SchedulableTask(
            f"chain-{link}", ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256),
            10.0, dependencies=() if link == 1 else (f"chain-{link - 1}",)))
    tasks.append(SchedulableTask(
        "lonely-small-task", ResourceEnvelopeSpec(cpu_cores=1, memory_mb=256),
        1.0))
    site = ExecutionSite(
        "private-node", ResourceEnvelopeSpec(cpu_cores=1, memory_mb=1024),
        network_classes=("none",))
    return Stage8Fixture(tuple(tasks), (site,), 40.0,
                         tuple(f"chain-{i}" for i in range(1, 5)))


def make_affinity_sites() -> tuple[ExecutionSite, ...]:
    """Two sites where only one carries the environment some work needs."""
    return (
        ExecutionSite("plain-node",
                      ResourceEnvelopeSpec(cpu_cores=8, memory_mb=8192),
                      network_classes=("none",)),
        ExecutionSite("gdal-node",
                      ResourceEnvelopeSpec(cpu_cores=4, memory_mb=4096),
                      environment_classes=("gdal",),
                      network_classes=("none", "public-internet")),
    )


__all__ = [
    "CHAIN_DURATION_S",
    "CHAIN_LENGTH",
    "CRITICAL_PATH_S",
    "NODE_CORES",
    "SHORT_COUNT",
    "SHORT_DURATION_S",
    "Stage8Fixture",
    "make_affinity_sites",
    "make_imbalanced_graph",
    "make_starvation_graph",
]
