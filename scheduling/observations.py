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

import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from capabilities.implementation import _required_text
from engine.runtime.identity import strict_hash

from .resources import ResourceEnvelopeSpec

_DIMENSIONS = ("cpu_cores", "memory_mb", "gpus", "scratch_mb")


class EstimateSource(str, Enum):
    DECLARED = "DECLARED"
    OBSERVED = "OBSERVED"


class ObservationKind(str, Enum):
    """How an attempt ended, because it changes what its numbers mean.

    A completed attempt measures its peak usage.  An attempt killed by a
    resource limit does not: its peak is a *censored lower bound*, because we
    only know it wanted at least that much before being stopped.  Treating the
    two identically is how the most important evidence -- the OOM -- gets
    discarded as "just a failure".
    """

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"


@dataclass(frozen=True)
class TaskObservation:
    """What one attempt actually did, and how much that measurement means."""

    task_key: str
    duration_s: float
    peak: ResourceEnvelopeSpec
    kind: ObservationKind = ObservationKind.COMPLETED
    transferred_bytes: int = 0
    exhausted_dimension: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.task_key, "observation task_key")
        if (isinstance(self.duration_s, bool)
                or not isinstance(self.duration_s, (int, float))
                or not math.isfinite(float(self.duration_s))
                or self.duration_s < 0):
            raise ValueError(
                "observed duration must be a finite non-negative number")
        if not isinstance(self.peak, ResourceEnvelopeSpec):
            raise TypeError("observed peak must be a ResourceEnvelopeSpec")
        if not isinstance(self.kind, ObservationKind):
            raise TypeError("observation kind must be typed")
        if (isinstance(self.transferred_bytes, bool)
                or not isinstance(self.transferred_bytes, int)
                or self.transferred_bytes < 0):
            raise ValueError("transferred_bytes must be a non-negative int")
        if self.kind is ObservationKind.RESOURCE_EXHAUSTED:
            if self.exhausted_dimension not in _DIMENSIONS:
                raise ValueError(
                    "a resource-exhausted attempt must name the dimension it "
                    f"hit, one of {sorted(_DIMENSIONS)}")
        elif self.exhausted_dimension is not None:
            raise ValueError(
                "only a resource-exhausted attempt names a dimension")

    @classmethod
    def completed(cls, task_key: str, duration_s: float, *,
                  memory_mb: int, cpu_cores: int = 1, gpus: int = 0,
                  scratch_mb: int = 0,
                  transferred_bytes: int = 0) -> "TaskObservation":
        return cls(task_key, duration_s,
                   ResourceEnvelopeSpec(cpu_cores, memory_mb, gpus, scratch_mb),
                   ObservationKind.COMPLETED, transferred_bytes)

    @classmethod
    def resource_exhausted(cls, task_key: str, duration_s: float, *,
                           dimension: str, memory_mb: int, cpu_cores: int = 1,
                           gpus: int = 0,
                           scratch_mb: int = 0) -> "TaskObservation":
        """An attempt stopped by a limit; its peak is a lower bound."""
        return cls(task_key, duration_s,
                   ResourceEnvelopeSpec(cpu_cores, memory_mb, gpus, scratch_mb),
                   ObservationKind.RESOURCE_EXHAUSTED, 0, dimension)

    @property
    def succeeded(self) -> bool:
        return self.kind is ObservationKind.COMPLETED

    @property
    def failed(self) -> bool:
        return self.kind is not ObservationKind.COMPLETED

    @property
    def censored(self) -> bool:
        """True when the peak is a lower bound rather than a measurement."""
        return self.kind is ObservationKind.RESOURCE_EXHAUSTED

    @property
    def peak_memory_mb(self) -> int:
        return self.peak.memory_mb

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "duration_s": self.duration_s,
            "peak": self.peak.to_dict(),
            "kind": self.kind.value,
            "transferred_bytes": self.transferred_bytes,
            "exhausted_dimension": self.exhausted_dimension,
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
    dimensions: tuple[str, ...] = ("memory_mb",)
    from_censored_evidence: bool = False

    def __post_init__(self) -> None:
        _required_text(self.task_key, "revision task_key")
        _required_text(self.reason, "revision reason")
        for value, label in ((self.declared, "declared"),
                             (self.proposed, "proposed")):
            if not isinstance(value, ResourceEnvelopeSpec):
                raise TypeError(f"revision {label} envelope is invalid")
        if not self.dimensions or any(item not in _DIMENSIONS
                                      for item in self.dimensions):
            raise ValueError("a revision must name real resource dimensions")
        if self.dimensions != tuple(sorted(set(self.dimensions))):
            raise ValueError("revision dimensions must be unique and sorted")
        if self.revision_id != self.expected_id():
            raise ValueError("deployment revision identity does not verify")

    @classmethod
    def bind(cls, *, task_key: str, declared: ResourceEnvelopeSpec,
             observed_peak_memory_mb: int, proposed: ResourceEnvelopeSpec,
             reason: str, dimensions: tuple[str, ...] = ("memory_mb",),
             from_censored_evidence: bool = False) -> "DeploymentRevision":
        ordered = tuple(sorted(set(dimensions)))
        return cls(
            strict_hash(cls._payload(
                task_key, declared, observed_peak_memory_mb, proposed, reason,
                ordered, from_censored_evidence)),
            task_key, declared, observed_peak_memory_mb, proposed, reason,
            ordered, from_censored_evidence)

    @staticmethod
    def _payload(task_key: str, declared: ResourceEnvelopeSpec,
                 observed_peak_memory_mb: int, proposed: ResourceEnvelopeSpec,
                 reason: str, dimensions: tuple[str, ...],
                 from_censored_evidence: bool) -> dict[str, Any]:
        return {
            "schema": "stage8-deployment-revision-v2",
            "task_key": task_key,
            "declared": declared.to_dict(),
            "observed_peak_memory_mb": observed_peak_memory_mb,
            "proposed": proposed.to_dict(),
            "reason": reason,
            "dimensions": list(dimensions),
            # A censored proposal is a floor, not a sizing: the attempt was
            # stopped, so the real requirement may be higher still.
            "from_censored_evidence": from_censored_evidence,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.task_key, self.declared, self.observed_peak_memory_mb,
            self.proposed, self.reason, self.dimensions,
            self.from_censored_evidence))

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.task_key, self.declared, self.observed_peak_memory_mb,
            self.proposed, self.reason, self.dimensions,
            self.from_censored_evidence)
        payload["revision_id"] = self.revision_id
        return payload


class ObservationHistory:
    """Accumulates observations and estimates only when it has enough of them."""

    def __init__(self, *, minimum_samples: int = 3,
                 memory_headroom: float = 1.25) -> None:
        if (isinstance(minimum_samples, bool)
                or not isinstance(minimum_samples, int) or minimum_samples < 1):
            raise ValueError("minimum_samples must be a positive integer")
        if (isinstance(memory_headroom, bool)
                or not isinstance(memory_headroom, (int, float))
                or not math.isfinite(float(memory_headroom))
                or memory_headroom < 1.0):
            raise ValueError(
                "memory headroom must be a finite number of at least one")
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
                   in self._observations.get(task_key, ()) if item.succeeded]
        if len(samples) < self.minimum_samples:
            return Estimate(declared_s, EstimateSource.DECLARED, len(samples))
        return Estimate(statistics.median(samples), EstimateSource.OBSERVED,
                        len(samples))

    def memory_estimate(self, task_key: str, declared_mb: int) -> Estimate:
        samples = [item.peak.memory_mb for item
                   in self._observations.get(task_key, ()) if item.succeeded]
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

        Attempts killed by a resource limit are the *most* informative
        evidence here and were previously discarded along with ordinary
        failures. They are included, and the resulting proposal is flagged as
        resting on censored evidence: the attempt was stopped, so the true
        requirement may be higher than the bound we saw.

        All four dimensions are checked, not just memory. Returns ``None``
        when the declared envelope held. The revision is a *proposal*: this
        layer never mutates the declared envelope itself.
        """
        samples = [item for item in self._observations.get(task_key, ())
                   if item.succeeded or item.censored]
        if not samples:
            return None
        censored = any(item.censored for item in samples)
        exceeded: list[str] = []
        proposed_values: dict[str, int] = {
            name: getattr(declared, name) for name in _DIMENSIONS}
        for name in _DIMENSIONS:
            observed = max(getattr(item.peak, name) for item in samples)
            if observed > getattr(declared, name):
                exceeded.append(name)
                # Headroom belongs on the continuous dimensions.  Cores and
                # GPUs are discrete counts: a task that used 2 GPUs needs 2,
                # and padding to 3 would reserve hardware nobody asked for.
                proposed_values[name] = (
                    observed if name in ("cpu_cores", "gpus")
                    else int(observed * self.memory_headroom) + 1)
        if not exceeded:
            return None
        observed_memory = max(item.peak.memory_mb for item in samples)
        detail = ", ".join(
            f"{name} {max(getattr(item.peak, name) for item in samples)} > "
            f"{getattr(declared, name)}" for name in exceeded)
        revision = DeploymentRevision.bind(
            task_key=task_key, declared=declared,
            observed_peak_memory_mb=observed_memory,
            proposed=ResourceEnvelopeSpec(**proposed_values),
            reason=(
                f"observed usage exceeded the declared envelope ({detail})"
                + (" from an attempt stopped by a resource limit, so the true "
                   "requirement may be higher still" if censored else "")),
            dimensions=tuple(exceeded),
            from_censored_evidence=censored)
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
