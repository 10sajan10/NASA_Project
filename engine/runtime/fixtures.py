"""Small, consequence-free graphs used to prove the Stage-1 kernel.

These fixtures deliberately contain no scientific resolver, remote data source,
MPI command, scheduler command, or WRF operation.  They exercise the runtime
boundary that later stages compile into.
"""
from __future__ import annotations

from pathlib import Path

from .operations import operation_component
from .types import (
    BoundExecutionGraph,
    InputBinding,
    OutputSpec,
    ResourceRequest,
    TaskTemplate,
)


_JSON_OUTPUT = OutputSpec(
    name="result",
    media_type="application/json",
    validation={"kind": "finite_json"},
)


def two_task_graph(*, value: int | float = 21,
                   factor: int | float = 2) -> BoundExecutionGraph:
    """Return the fixed ``constant -> scale`` Stage-1 acceptance graph."""

    producer = TaskTemplate(
        key="synthetic-producer",
        component=operation_component("synthetic.constant.v1"),
        parameters={"value": value},
        outputs=(_JSON_OUTPUT,),
        resources=ResourceRequest(cpu_cores=1, memory_mb=64, walltime_s=30),
    )
    consumer = TaskTemplate(
        key="deterministic-consumer",
        component=operation_component("synthetic.scale.v1"),
        parameters={"factor": factor},
        inputs=(InputBinding(
            input_name="value",
            upstream_task=producer.key,
            upstream_output="result",
        ),),
        outputs=(_JSON_OUTPUT,),
        resources=ResourceRequest(cpu_cores=1, memory_mb=64, walltime_s=30),
    )
    return BoundExecutionGraph.bind(
        "stage1-two-task-fixture",
        (producer, consumer),
    )


def sleeping_graph(*, seconds: float, marker: Path | None = None,
                   value: int | float = 7) -> BoundExecutionGraph:
    """Return one supervised subprocess task for restart/cancel tests."""

    parameters: dict[str, object] = {"seconds": seconds, "value": value}
    if marker is not None:
        parameters["started_marker"] = str(marker)
    return BoundExecutionGraph.bind(
        "stage1-sleep-fixture",
        (TaskTemplate(
            key="supervised-sleep",
            component=operation_component("synthetic.sleep.v1"),
            parameters=parameters,
            outputs=(_JSON_OUTPUT,),
            resources=ResourceRequest(
                cpu_cores=1,
                memory_mb=64,
                walltime_s=max(seconds + 5, 10),
            ),
        ),),
    )


def retry_graph(*, marker: Path, retry_delay_s: float = 0.05,
                value: int | float = 9) -> BoundExecutionGraph:
    """Return a task that fails once using a node-local test marker."""

    return BoundExecutionGraph.bind(
        "stage1-retry-fixture",
        (TaskTemplate(
            key="retry-safe-fail-once",
            component=operation_component("synthetic.fail_once.v1"),
            parameters={"marker": str(marker), "value": value},
            outputs=(_JSON_OUTPUT,),
            resources=ResourceRequest(cpu_cores=1, memory_mb=64, walltime_s=10),
            max_attempts=2,
            retry_delay_s=retry_delay_s,
        ),),
    )
