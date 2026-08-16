"""Measured history, and what to do when a declared envelope was wrong.

Two rules from the roadmap, both about honesty rather than performance:

* **Declared estimates are used until there is enough history to beat them.**
  One sample is not a distribution. Below a declared minimum the estimator
  returns the declared value and says the source was ``DECLARED``, so nobody
  mistakes a guess for a measurement.
* **Resource underestimation is never an unrecorded mutation.** If a task
  actually needed more than it declared, the scheduler emits a
  :class:`DeploymentRevision` — an explicit, identified record of what changed
  and why — rather than quietly widening the envelope next time.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from capabilities.implementation import _required_text
from engine.runtime.identity import strict_hash

from .resources import ResourceEnvelopeSpec


class EstimateSource(str, Enum):
    DECLARED = "DECLARED"
    OBSERVED = "OBSERVED"


@dataclass(frozen=True)
class TaskObservation:
    """What one attempt actually did."""

    task_key: str
    duration_s: float
    peak_memory_mb: int
    transferred_bytes: int = 0
    failed: bool = False

    def __post_init__(self) -> None:
        _required_text(self.task_key, "observation task_key")
        if (isinstance(self.duration_s, bool)
                or not isinstance(self.duration_s, (int, float))
                or self.duration_s < 0):
            raise ValueError("observed duration must be non-negative")
        for name in ("peak_memory_mb", "transferred_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"observed {name} must be a non-negative int")
        if type(self.failed) is not bool:
            raise TypeError("failed must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "duration_s": self.duration_s,
            "peak_memory_mb": self.peak_memory_mb,
            "transferred_bytes": self.transferred_bytes,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class Estimate:
    """A predicted value plus an honest statement of where it came from."""

    value: float
    source: EstimateSource
    sample_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.source, EstimateSource):
            raise TypeError("estimate source must be typed")
        if (isinstance(self.sample_count, bool)
                or not isinstance(self.sample_count, int)
                or self.sample_count < 0):
            raise ValueError("sample count must be a non-negative integer")
        if self.source is EstimateSource.DECLARED and self.sample_count < 0:
            raise ValueError("declared estimates cannot have negative samples")

    @property
    def measured(self) -> bool:
        return self.source is EstimateSource.OBSERVED

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source.value,
                "sample_count": self.sample_count}


@dataclass(frozen=True)
class DeploymentRevision:
    """An explicit record that a declared envelope did not hold.

    This exists so an underestimate produces a *decision trail*. Silently
    enlarging the envelope on the next run would make the original plan
    unreproducible and hide a real modelling error.
    """

    revision_id: str
    task_key: str
    declared: ResourceEnvelopeSpec
    observed_peak_memory_mb: int
    proposed: ResourceEnvelopeSpec
    reason: str

    def __post_init__(self) -> None:
        _required_text(self.task_key, "revision task_key")
        _required_text(self.reason, "revision reason")
        for value, label in ((self.declared, "declared"),
                             (self.proposed, "proposed")):
            if not isinstance(value, ResourceEnvelopeSpec):
                raise TypeError(f"revision {label} envelope is invalid")
        if self.revision_id != self.expected_id():
            raise ValueError("deployment revision identity does not verify")

    @classmethod
    def bind(cls, *, task_key: str, declared: ResourceEnvelopeSpec,
             observed_peak_memory_mb: int, proposed: ResourceEnvelopeSpec,
             reason: str) -> "DeploymentRevision":
        return cls(
            strict_hash(cls._payload(task_key, declared,
                                     observed_peak_memory_mb, proposed, reason)),
            task_key, declared, observed_peak_memory_mb, proposed, reason)

    @staticmethod
    def _payload(task_key: str, declared: ResourceEnvelopeSpec,
                 observed_peak_memory_mb: int, proposed: ResourceEnvelopeSpec,
                 reason: str) -> dict[str, Any]:
        return {
            "schema": "stage8-deployment-revision-v1",
            "task_key": task_key,
            "declared": declared.to_dict(),
            "observed_peak_memory_mb": observed_peak_memory_mb,
            "proposed": proposed.to_dict(),
            "reason": reason,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.task_key, self.declared, self.observed_peak_memory_mb,
            self.proposed, self.reason))

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.task_key, self.declared, self.observed_peak_memory_mb,
            self.proposed, self.reason)
        payload["revision_id"] = self.revision_id
        return payload


class ObservationHistory:
    """Accumulates observations and estimates only when it has enough of them."""

    def __init__(self, *, minimum_samples: int = 3,
                 memory_headroom: float = 1.25) -> None:
        if (isinstance(minimum_samples, bool)
                or not isinstance(minimum_samples, int) or minimum_samples < 1):
            raise ValueError("minimum_samples must be a positive integer")
        if memory_headroom < 1.0:
            raise ValueError("memory headroom cannot shrink an envelope")
        self.minimum_samples = minimum_samples
        self.memory_headroom = memory_headroom
        self._observations: dict[str, list[TaskObservation]] = {}
        self._revisions: list[DeploymentRevision] = []

    def record(self, observation: TaskObservation) -> None:
        if not isinstance(observation, TaskObservation):
            raise TypeError("record requires a TaskObservation")
        self._observations.setdefault(observation.task_key, []).append(
            observation)

    def record_all(self, observations: Iterable[TaskObservation]) -> None:
        for observation in observations:
            self.record(observation)

    def sample_count(self, task_key: str) -> int:
        return len(self._observations.get(task_key, ()))

    def duration_estimate(self, task_key: str,
                          declared_s: float) -> Estimate:
        """Median observed duration once there is enough history, else declared.

        Median rather than mean: one pathological run should not move the
        estimate far, and scheduling is more sensitive to the typical case.
        Only successful attempts count -- a task that failed fast is not fast.
        """
        samples = [item.duration_s for item
                   in self._observations.get(task_key, ()) if not item.failed]
        if len(samples) < self.minimum_samples:
            return Estimate(declared_s, EstimateSource.DECLARED, len(samples))
        return Estimate(statistics.median(samples), EstimateSource.OBSERVED,
                        len(samples))

    def memory_estimate(self, task_key: str, declared_mb: int) -> Estimate:
        samples = [item.peak_memory_mb for item
                   in self._observations.get(task_key, ()) if not item.failed]
        if len(samples) < self.minimum_samples:
            return Estimate(float(declared_mb), EstimateSource.DECLARED,
                            len(samples))
        return Estimate(float(max(samples)), EstimateSource.OBSERVED,
                        len(samples))

    def failure_rate(self, task_key: str) -> Estimate:
        samples = self._observations.get(task_key, ())
        if len(samples) < self.minimum_samples:
            return Estimate(0.0, EstimateSource.DECLARED, len(samples))
        failures = sum(1 for item in samples if item.failed)
        return Estimate(failures / len(samples), EstimateSource.OBSERVED,
                        len(samples))

    def review_envelope(self, task_key: str,
                        declared: ResourceEnvelopeSpec
                        ) -> DeploymentRevision | None:
        """Emit a revision when observed usage exceeded what was declared.

        Returns ``None`` when the declared envelope held. The revision is a
        *proposal*: this layer never mutates the declared envelope itself.
        """
        samples = [item.peak_memory_mb for item
                   in self._observations.get(task_key, ()) if not item.failed]
        if not samples:
            return None
        observed = max(samples)
        if observed <= declared.memory_mb:
            return None
        proposed_mb = int(observed * self.memory_headroom) + 1
        revision = DeploymentRevision.bind(
            task_key=task_key, declared=declared,
            observed_peak_memory_mb=observed,
            proposed=ResourceEnvelopeSpec(
                cpu_cores=declared.cpu_cores, memory_mb=proposed_mb,
                gpus=declared.gpus, scratch_mb=declared.scratch_mb),
            reason=(
                f"observed peak memory {observed} MB exceeded the declared "
                f"{declared.memory_mb} MB"))
        self._revisions.append(revision)
        return revision

    @property
    def revisions(self) -> tuple[DeploymentRevision, ...]:
        return tuple(self._revisions)


__all__ = [
    "DeploymentRevision",
    "Estimate",
    "EstimateSource",
    "ObservationHistory",
    "TaskObservation",
]
