"""Immutable identities and state enums for the Stage-1 runtime."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .identity import freeze_json, require_object_fields, strict_copy, strict_hash


_HEX_DIGEST_LENGTH = 64


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _digest(value: str, label: str) -> None:
    _required_text(value, label)
    if (len(value) != _HEX_DIGEST_LENGTH
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


def _strict_dataclass(value: dict[str, Any], cls: type,
                      context: str) -> dict[str, Any]:
    return require_object_fields(
        value,
        {field.name for field in dataclasses.fields(cls)},
        context,
    )


class RunState(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskState(str, Enum):
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    READY = "READY"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    COMMITTING = "COMMITTING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    LOST = "LOST"


class AttemptState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    RESULT_READY = "RESULT_READY"
    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"
    LOST = "LOST"
    CANCELLED = "CANCELLED"


class WakeKind(str, Enum):
    DEPENDENCY_COMMIT = "DEPENDENCY_COMMIT"
    RETRY_TIMER = "RETRY_TIMER"
    ATTEMPT_RECONCILIATION = "ATTEMPT_RECONCILIATION"
    DEADLINE = "DEADLINE"


@dataclass(frozen=True)
class ResourceRequest:
    cpu_cores: int = 1
    memory_mb: int = 128
    gpus: int = 0
    mpi_ranks: int = 0
    walltime_s: float = 60.0

    def __post_init__(self) -> None:
        if (isinstance(self.cpu_cores, bool)
                or not isinstance(self.cpu_cores, int)
                or isinstance(self.memory_mb, bool)
                or not isinstance(self.memory_mb, int)
                or isinstance(self.gpus, bool)
                or not isinstance(self.gpus, int)
                or isinstance(self.mpi_ranks, bool)
                or not isinstance(self.mpi_ranks, int)
                or isinstance(self.walltime_s, bool)
                or not isinstance(self.walltime_s, (int, float))):
            raise TypeError("resource amounts must be numeric with integer counts")
        if (self.cpu_cores < 1 or self.memory_mb < 1
                or not math.isfinite(float(self.walltime_s))
                or self.walltime_s <= 0):
            raise ValueError("CPU, memory, and walltime must be positive")
        if self.gpus < 0 or self.mpi_ranks < 0:
            raise ValueError("GPU and MPI requests cannot be negative")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResourceRequest":
        return cls(**_strict_dataclass(value, cls, "ResourceRequest"))


@dataclass(frozen=True)
class ExecutableComponent:
    """Exact executable implementation selected before runtime."""

    component_id: str
    version: str
    implementation_digest: str
    operation_key: str
    deterministic: bool = False
    idempotent: bool = False
    retry_safe: bool = False

    def __post_init__(self) -> None:
        if not all((self.component_id, self.version,
                    self.implementation_digest, self.operation_key)):
            raise ValueError("component identity fields cannot be empty")
        _digest(self.implementation_digest, "implementation_digest")
        for label in ("deterministic", "idempotent", "retry_safe"):
            if type(getattr(self, label)) is not bool:
                raise TypeError(f"component {label} must be bool")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutableComponent":
        return cls(**_strict_dataclass(value, cls, "ExecutableComponent"))


@dataclass(frozen=True)
class InputBinding:
    input_name: str
    upstream_task: str
    upstream_output: str = "result"

    def __post_init__(self) -> None:
        _required_text(self.input_name, "input_name")
        _required_text(self.upstream_task, "upstream_task")
        _required_text(self.upstream_output, "upstream_output")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InputBinding":
        return cls(**_strict_dataclass(value, cls, "InputBinding"))


@dataclass(frozen=True)
class OutputSpec:
    name: str = "result"
    media_type: str = "application/json"
    validation: dict[str, Any] = field(
        default_factory=lambda: {"kind": "finite_json"})

    def __post_init__(self) -> None:
        _required_text(self.name, "output name")
        _required_text(self.media_type, "output media_type")
        object.__setattr__(self, "validation", freeze_json(self.validation))
        if not isinstance(self.validation, dict):
            raise ValueError("output validation must be a JSON object")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OutputSpec":
        return cls(**_strict_dataclass(value, cls, "OutputSpec"))


@dataclass(frozen=True)
class TaskTemplate:
    key: str
    component: ExecutableComponent
    parameters: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[InputBinding, ...] = ()
    outputs: tuple[OutputSpec, ...] = field(
        default_factory=lambda: (OutputSpec(),))
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    max_attempts: int = 1
    retry_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("task key cannot be empty")
        if not self.outputs:
            raise ValueError("task must declare at least one output")
        if len({o.name for o in self.outputs}) != len(self.outputs):
            raise ValueError(f"task {self.key!r} has duplicate output names")
        if (isinstance(self.max_attempts, bool)
                or not isinstance(self.max_attempts, int)
                or self.max_attempts < 1
                or isinstance(self.retry_delay_s, bool)
                or not isinstance(self.retry_delay_s, (int, float))
                or not math.isfinite(float(self.retry_delay_s))
                or self.retry_delay_s < 0):
            raise ValueError("invalid retry policy")
        if self.max_attempts > 1 and not self.component.retry_safe:
            raise ValueError("non-retry-safe component cannot request retries")
        if len({i.input_name for i in self.inputs}) != len(self.inputs):
            raise ValueError(f"task {self.key!r} has duplicate input names")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        if not isinstance(self.parameters, dict):
            raise ValueError("task parameters must be a JSON object")
        for output in self.outputs:
            strict_copy(output.validation)

    def identity_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class ArtifactRecipe:
    recipe_id: str
    task_id: str
    output_name: str
    media_type: str
    validation: dict[str, Any]

    def __post_init__(self) -> None:
        _digest(self.recipe_id, "recipe_id")
        _digest(self.task_id, "artifact recipe task_id")
        _required_text(self.output_name, "artifact output_name")
        _required_text(self.media_type, "artifact media_type")
        object.__setattr__(self, "validation", freeze_json(self.validation))
        if not isinstance(self.validation, dict):
            raise ValueError("artifact validation must be a JSON object")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactRecipe":
        return cls(**_strict_dataclass(value, cls, "ArtifactRecipe"))

    @classmethod
    def bind(cls, plan_id: str, task_id: str,
             output: OutputSpec) -> "ArtifactRecipe":
        identity = {
            "schema": "stage1-artifact-recipe-v1",
            "plan_id": plan_id,
            "task_id": task_id,
            "output": dataclasses.asdict(output),
        }
        return cls(
            recipe_id=strict_hash(identity),
            task_id=task_id,
            output_name=output.name,
            media_type=output.media_type,
            validation=strict_copy(output.validation),
        )


@dataclass(frozen=True)
class BoundTask:
    task_id: str
    key: str
    component: ExecutableComponent
    parameters: dict[str, Any]
    inputs: tuple[InputBinding, ...]
    outputs: tuple[ArtifactRecipe, ...]
    resources: ResourceRequest
    max_attempts: int
    retry_delay_s: float

    def __post_init__(self) -> None:
        _digest(self.task_id, "task_id")
        _required_text(self.key, "task key")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        if not isinstance(self.parameters, dict):
            raise ValueError("bound task parameters must be a JSON object")
        if not self.outputs:
            raise ValueError("bound task must declare at least one output")
        if len({recipe.output_name for recipe in self.outputs}) != len(self.outputs):
            raise ValueError("bound task has duplicate output names")
        if len({binding.input_name for binding in self.inputs}) != len(self.inputs):
            raise ValueError("bound task has duplicate input names")
        if (not isinstance(self.inputs, tuple)
                or not all(isinstance(value, InputBinding)
                           for value in self.inputs)
                or not isinstance(self.outputs, tuple)
                or not all(isinstance(value, ArtifactRecipe)
                           for value in self.outputs)
                or not isinstance(self.component, ExecutableComponent)
                or not isinstance(self.resources, ResourceRequest)):
            raise TypeError("bound task contains an invalid typed field")
        if (isinstance(self.max_attempts, bool)
                or not isinstance(self.max_attempts, int)
                or self.max_attempts < 1
                or isinstance(self.retry_delay_s, bool)
                or not isinstance(self.retry_delay_s, (int, float))
                or not math.isfinite(float(self.retry_delay_s))
                or self.retry_delay_s < 0):
            raise ValueError("invalid bound-task retry policy")
        if self.max_attempts > 1 and not self.component.retry_safe:
            raise ValueError("non-retry-safe component cannot request retries")
        if any(recipe.task_id != self.task_id for recipe in self.outputs):
            raise ValueError("artifact recipe is bound to another task")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundTask":
        raw = _strict_dataclass(value, cls, "BoundTask")
        raw["component"] = ExecutableComponent.from_dict(raw["component"])
        if not isinstance(raw["inputs"], list):
            raise ValueError("BoundTask.inputs must be a JSON array")
        if not isinstance(raw["outputs"], list):
            raise ValueError("BoundTask.outputs must be a JSON array")
        raw["inputs"] = tuple(InputBinding.from_dict(v) for v in raw["inputs"])
        raw["outputs"] = tuple(ArtifactRecipe.from_dict(v) for v in raw["outputs"])
        raw["resources"] = ResourceRequest.from_dict(raw["resources"])
        return cls(**raw)


@dataclass(frozen=True)
class BoundExecutionGraph:
    """A finite immutable graph accepted by Stage 1.

    The scientific meaning is intentionally opaque. Stage 2 will introduce the
    scientific requirement/capability types and compile them to this boundary.
    """

    name: str
    schema_version: str
    plan_id: str
    tasks: tuple[BoundTask, ...]
    external_manifest_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.name, "graph name")
        _required_text(self.schema_version, "graph schema_version")
        _digest(self.plan_id, "plan_id")
        if (not isinstance(self.tasks, tuple)
                or not all(isinstance(task, BoundTask) for task in self.tasks)
                or not self.tasks):
            raise ValueError("bound graph must contain tasks")
        if len({task.task_id for task in self.tasks}) != len(self.tasks):
            raise ValueError("bound graph has duplicate task IDs")
        if len({task.key for task in self.tasks}) != len(self.tasks):
            raise ValueError("bound graph has duplicate task keys")
        if (not isinstance(self.external_manifest_roots, tuple)
                or any(not isinstance(root, str) or not root for root
                       in self.external_manifest_roots)):
            raise ValueError("external manifest roots must be non-empty strings")

    @classmethod
    def bind(cls, name: str, templates: Iterable[TaskTemplate], *,
             schema_version: str = "stage1-manual-bound-graph-v1",
             external_manifest_roots: Iterable[str] = ()) -> "BoundExecutionGraph":
        templates_t = tuple(templates)
        keys = [task.key for task in templates_t]
        if (not templates_t or any(not isinstance(key, str) or not key
                                   for key in keys)
                or len(keys) != len(set(keys))):
            raise ValueError("graph must have unique, non-empty tasks")
        key_set = set(keys)
        for task in templates_t:
            upstream_outputs = {
                candidate.key: {output.name for output in candidate.outputs}
                for candidate in templates_t
            }
            for binding in task.inputs:
                if binding.upstream_task not in key_set:
                    raise ValueError(
                        f"{task.key!r} references unknown upstream task "
                        f"{binding.upstream_task!r}")
                if binding.upstream_output not in upstream_outputs[
                        binding.upstream_task]:
                    raise ValueError(
                        f"{task.key!r} references missing output "
                        f"{binding.upstream_task}.{binding.upstream_output}")
        _validate_acyclic(templates_t)
        roots = tuple(external_manifest_roots)
        if len(roots) != len(set(roots)):
            raise ValueError("external manifest roots must be unique")
        plan_payload = {
            "schema": schema_version,
            "name": name,
            "tasks": [task.identity_dict()
                      for task in sorted(templates_t, key=lambda t: t.key)],
            "external_manifest_roots": roots,
        }
        plan_id = strict_hash(plan_payload)
        task_ids = {
            task.key: strict_hash({
                "schema": "stage1-logical-task-v1",
                "plan_id": plan_id,
                "task_key": task.key,
                "component": dataclasses.asdict(task.component),
                "inputs": [dataclasses.asdict(v) for v in task.inputs],
                "parameters": task.parameters,
            })
            for task in templates_t
        }
        bound: list[BoundTask] = []
        for task in sorted(templates_t, key=lambda t: t.key):
            task_id = task_ids[task.key]
            bound.append(BoundTask(
                task_id=task_id,
                key=task.key,
                component=task.component,
                parameters=strict_copy(task.parameters),
                inputs=tuple(task.inputs),
                outputs=tuple(
                    ArtifactRecipe.bind(plan_id, task_id, output)
                    for output in task.outputs),
                resources=task.resources,
                max_attempts=task.max_attempts,
                retry_delay_s=task.retry_delay_s,
            ))
        return cls(name, schema_version, plan_id, tuple(bound), roots)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BoundExecutionGraph":
        raw = _strict_dataclass(value, cls, "BoundExecutionGraph")
        if not isinstance(raw["tasks"], list):
            raise ValueError("BoundExecutionGraph.tasks must be a JSON array")
        if not isinstance(raw["external_manifest_roots"], list):
            raise ValueError(
                "BoundExecutionGraph.external_manifest_roots must be a JSON array")
        raw["tasks"] = tuple(BoundTask.from_dict(v) for v in raw["tasks"])
        raw["external_manifest_roots"] = tuple(raw["external_manifest_roots"])
        graph = cls(**raw)
        graph.validate_identity()
        return graph

    def validate_identity(self) -> None:
        templates = tuple(TaskTemplate(
            key=task.key,
            component=task.component,
            parameters=task.parameters,
            inputs=task.inputs,
            outputs=tuple(OutputSpec(
                name=recipe.output_name,
                media_type=recipe.media_type,
                validation=recipe.validation,
            ) for recipe in task.outputs),
            resources=task.resources,
            max_attempts=task.max_attempts,
            retry_delay_s=task.retry_delay_s,
        ) for task in self.tasks)
        rebuilt = BoundExecutionGraph.bind(
            self.name,
            templates,
            schema_version=self.schema_version,
            external_manifest_roots=self.external_manifest_roots,
        )
        if rebuilt.to_dict() != self.to_dict():
            raise ValueError("bound execution graph identity does not verify")

    def task_by_id(self, task_id: str) -> BoundTask:
        try:
            return next(task for task in self.tasks if task.task_id == task_id)
        except StopIteration as exc:
            raise KeyError(task_id) from exc

    def task_by_key(self, key: str) -> BoundTask:
        try:
            return next(task for task in self.tasks if task.key == key)
        except StopIteration as exc:
            raise KeyError(key) from exc


@dataclass(frozen=True)
class SiteSnapshot:
    site_id: str
    hostname: str
    cpuset: tuple[int, ...]
    memory_mb: int
    gpu_ids: tuple[str, ...]
    allocation_expires_at: float | None
    source: str = "current-process-envelope"

    def __post_init__(self) -> None:
        _required_text(self.site_id, "site_id")
        _required_text(self.hostname, "hostname")
        _required_text(self.source, "site source")
        if not self.cpuset or len(set(self.cpuset)) != len(self.cpuset):
            raise ValueError("site cpuset must contain unique CPU IDs")
        if (any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0
                for cpu in self.cpuset)
                or any(not isinstance(gpu, str) or not gpu for gpu in self.gpu_ids)):
            raise ValueError("site resource identifiers are invalid")
        if (isinstance(self.memory_mb, bool)
                or not isinstance(self.memory_mb, int)
                or self.memory_mb < 1):
            raise ValueError("site memory_mb must be positive")
        if (self.allocation_expires_at is not None
                and (isinstance(self.allocation_expires_at, bool)
                     or not isinstance(self.allocation_expires_at, (int, float))
                     or not math.isfinite(float(self.allocation_expires_at)))):
            raise ValueError("allocation expiry must be finite or absent")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SiteSnapshot":
        raw = _strict_dataclass(value, cls, "SiteSnapshot")
        raw["cpuset"] = tuple(raw["cpuset"])
        raw["gpu_ids"] = tuple(raw["gpu_ids"])
        return cls(**raw)

    @property
    def snapshot_id(self) -> str:
        return strict_hash(dataclasses.asdict(self))


@dataclass(frozen=True)
class AttemptSpec:
    run_id: str
    deployment_id: str
    task: BoundTask
    attempt_id: str
    attempt_number: int
    fencing_token: int
    provider: str
    input_artifacts: dict[str, str]
    stage_dir: str
    created_at: float

    def __post_init__(self) -> None:
        _required_text(self.run_id, "attempt run_id")
        _digest(self.deployment_id, "deployment_id")
        _digest(self.attempt_id, "attempt_id")
        _required_text(self.provider, "attempt provider")
        _required_text(self.stage_dir, "attempt stage_dir")
        if (isinstance(self.attempt_number, bool)
                or not isinstance(self.attempt_number, int)
                or self.attempt_number < 1
                or isinstance(self.fencing_token, bool)
                or not isinstance(self.fencing_token, int)
                or self.fencing_token < 1):
            raise ValueError("attempt number and fencing token must be positive")
        if (isinstance(self.created_at, bool)
                or not isinstance(self.created_at, (int, float))
                or not math.isfinite(float(self.created_at))
                or self.created_at <= 0):
            raise ValueError("attempt created_at must be a positive finite timestamp")
        object.__setattr__(self, "input_artifacts", freeze_json(
            self.input_artifacts))
        if not isinstance(self.input_artifacts, dict) or any(
                not isinstance(key, str) or not isinstance(path, str)
                for key, path in self.input_artifacts.items()):
            raise ValueError("input artifacts must map string ports to paths")

    @property
    def attempt_token(self) -> str:
        return strict_hash({
            "run_id": self.run_id,
            "task_id": self.task.task_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
        })

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AttemptSpec":
        raw = _strict_dataclass(value, cls, "AttemptSpec")
        raw["task"] = BoundTask.from_dict(raw["task"])
        spec = cls(**raw)
        expected = attempt_id(
            spec.run_id, spec.task.task_id, spec.deployment_id,
            spec.attempt_number, spec.fencing_token)
        if spec.attempt_id != expected:
            raise ValueError("attempt identity does not verify")
        return spec


@dataclass(frozen=True)
class ExternalHandle:
    provider: str
    external_id: str
    attempt_id: str
    attempt_token: str
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        _required_text(self.provider, "handle provider")
        _required_text(self.external_id, "handle external_id")
        _digest(self.attempt_id, "handle attempt_id")
        _digest(self.attempt_token, "handle attempt_token")
        object.__setattr__(self, "metadata", freeze_json(self.metadata))
        if not isinstance(self.metadata, dict):
            raise ValueError("handle metadata must be a JSON object")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExternalHandle":
        return cls(**_strict_dataclass(value, cls, "ExternalHandle"))


@dataclass(frozen=True)
class ProviderObservation:
    attempt_id: str
    attempt_token: str
    state: AttemptState
    observed_at: float
    result_manifest_path: str | None = None
    error: str | None = None
    exit_code: int | None = None
    recovered_handle: ExternalHandle | None = None

    def __post_init__(self) -> None:
        _digest(self.attempt_id, "observation attempt_id")
        _digest(self.attempt_token, "observation attempt_token")
        if not isinstance(self.state, AttemptState):
            raise TypeError("observation state must be AttemptState")
        if (isinstance(self.observed_at, bool)
                or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(float(self.observed_at))
                or self.observed_at <= 0):
            raise ValueError("observation time must be a positive finite timestamp")
        if (self.exit_code is not None
                and (isinstance(self.exit_code, bool)
                     or not isinstance(self.exit_code, int))):
            raise TypeError("observation exit_code must be an integer or absent")
        if self.state is AttemptState.RESULT_READY:
            _required_text(
                self.result_manifest_path or "", "result manifest path")
        elif self.result_manifest_path is not None:
            raise ValueError(
                "only a RESULT_READY observation may carry a result manifest")
        if self.recovered_handle is not None and (
                self.recovered_handle.attempt_id != self.attempt_id
                or self.recovered_handle.attempt_token != self.attempt_token):
            raise ValueError("recovered handle does not match observation")


def deployment_id(plan_id: str, site: SiteSnapshot,
                  provider: str) -> str:
    return strict_hash({
        "schema": "stage1-deployment-binding-v1",
        "plan_id": plan_id,
        "site_snapshot_id": site.snapshot_id,
        "provider": provider,
    })


def attempt_id(run_id: str, task_id: str, deployment: str,
               attempt_number: int, fencing_token: int) -> str:
    return strict_hash({
        "schema": "stage1-attempt-v1",
        "run_id": run_id,
        "task_id": task_id,
        "deployment_id": deployment,
        "attempt_number": attempt_number,
        "fencing_token": fencing_token,
    })


def _validate_acyclic(tasks: tuple[TaskTemplate, ...]) -> None:
    deps = {task.key: {value.upstream_task for value in task.inputs}
            for task in tasks}
    remaining = dict(deps)
    while remaining:
        ready = [key for key, values in remaining.items() if not values]
        if not ready:
            raise ValueError(
                "cycle in bound execution graph: " + ", ".join(sorted(remaining)))
        for key in ready:
            remaining.pop(key)
        for values in remaining.values():
            values.difference_update(ready)
