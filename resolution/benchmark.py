"""Reproducible Stage-3 conformance-microbenchmark records.

This module deliberately does not import NumPy or SciPy at module import time.
Consequently, importing :mod:`resolution` does not initialize the numerical
stack merely because it exposes :class:`PlanningBenchmarkProfile`. Package and
embedded-HiGHS versions are collected lazily when a benchmark is measured.

The benchmark is a correctness-gated microbenchmark over a frozen conformance
graph. It is *not* evidence that the later, representative Stage-6 planning
latency SLO has been met.
"""
from __future__ import annotations

import os
import platform as platform_module
import statistics
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Callable

from engine.runtime.identity import (
    freeze_json,
    require_object_fields,
    strict_copy,
    strict_hash,
)

from .milp import MilpSolveOptions
from .service import ResolutionOutcome, ResolutionStatus


_SCHEMA = "stage3-planning-benchmark-profile-v2"
_BENCHMARK_SCOPE = "STAGE3_CONFORMANCE_MICROBENCHMARK"
_SLO_INTERPRETATION = "NOT_A_STAGE6_REPRESENTATIVE_SLO"
_TIMING_SOURCE = "PERF_COUNTER_NS_AROUND_FULL_RESOLVE_CALL"


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _nonnegative_integer(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


def _optional_nonnegative_integer(value: int | None, label: str) -> None:
    if value is not None:
        _nonnegative_integer(value, label)


def _p95(samples: tuple[int, ...]) -> int:
    return int(
        statistics.quantiles(samples, n=20, method="inclusive")[18]
        if len(samples) > 1 else samples[0]
    )


@dataclass(frozen=True)
class PlanningBenchmarkProfile:
    """Frozen context plus externally measured warm-process planning samples.

    Every measured call must independently return the same READY, validated,
    globally optimal graph/plan context. ``total_ns_samples`` measures around
    the entire caller-provided ``resolve`` function; ``resolver_total_ns_samples``
    preserves the service's narrower self-reported total for diagnosis.
    """

    benchmark_id: str
    benchmark_scope: str
    slo_interpretation: str
    timing_source: str
    graph_id: str
    selector_problem_id: str
    selection_problem_id: str
    selected_plan_id: str
    validation_report_id: str
    resolution_status: str
    selection_status: str
    solver_status_code: int | None
    discovery_complete: bool
    validation_passed: bool
    globally_optimal: bool
    eligible_for_binding: bool
    requirement_count: int
    use_count: int
    invocation_count: int
    artifact_count: int
    satisfaction_arc_count: int
    selected_derivation_depth: int
    warmup_runs: int
    measured_runs: int
    cache_state: str
    run_config: dict[str, Any]
    solve_options: dict[str, Any]
    python_version: str
    python_implementation: str
    platform: str
    machine_architecture: str
    cpu_model: str
    logical_cpu_count: int | None
    memory_total_bytes: int | None
    solver_name: str
    solver_version: str
    highs_version: str
    scipy_version: str
    numpy_version: str
    total_ns_samples: tuple[int, ...]
    resolver_total_ns_samples: tuple[int, ...]
    p95_total_ns: int
    p95_resolver_total_ns: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.graph_id, "benchmark graph identity"),
            (self.selector_problem_id, "selector problem identity"),
            (self.selection_problem_id, "selection problem identity"),
            (self.selected_plan_id, "selected plan identity"),
            (self.validation_report_id, "validation report identity"),
            (self.cache_state, "benchmark cache state"),
            (self.python_version, "Python version"),
            (self.python_implementation, "Python implementation"),
            (self.platform, "benchmark platform"),
            (self.machine_architecture, "machine architecture"),
            (self.cpu_model, "CPU model"),
            (self.solver_name, "solver name"),
            (self.solver_version, "solver version"),
            (self.highs_version, "HiGHS version"),
            (self.scipy_version, "SciPy version"),
            (self.numpy_version, "NumPy version"),
        ):
            _required_text(value, label)
        if self.benchmark_scope != _BENCHMARK_SCOPE:
            raise ValueError("unsupported benchmark scope")
        if self.slo_interpretation != _SLO_INTERPRETATION:
            raise ValueError("benchmark cannot claim the Stage-6 SLO")
        if self.timing_source != _TIMING_SOURCE:
            raise ValueError("benchmark timing source is unsupported")
        if self.resolution_status != ResolutionStatus.READY.value:
            raise ValueError("benchmark outcome must be READY")
        if self.selection_status != "OPTIMAL":
            raise ValueError("benchmark selection must be globally optimal")
        if self.solver_status_code is not None and (
                isinstance(self.solver_status_code, bool)
                or not isinstance(self.solver_status_code, int)):
            raise TypeError("solver status code must be an integer or None")
        for name in (
            "discovery_complete",
            "validation_passed",
            "globally_optimal",
            "eligible_for_binding",
        ):
            if getattr(self, name) is not True:
                raise ValueError(f"{name} must be true for a benchmark sample")
        for name in (
            "requirement_count",
            "use_count",
            "invocation_count",
            "artifact_count",
            "satisfaction_arc_count",
            "selected_derivation_depth",
            "warmup_runs",
            "measured_runs",
            "p95_total_ns",
            "p95_resolver_total_ns",
        ):
            _nonnegative_integer(getattr(self, name), name)
        if self.measured_runs < 1:
            raise ValueError("measured_runs must be positive")
        _optional_nonnegative_integer(
            self.logical_cpu_count, "logical_cpu_count")
        _optional_nonnegative_integer(
            self.memory_total_bytes, "memory_total_bytes")

        if not isinstance(self.run_config, dict):
            raise TypeError("run_config must be a JSON object")
        if not isinstance(self.solve_options, dict):
            raise TypeError("solve_options must be a JSON object")
        object.__setattr__(self, "run_config", freeze_json(self.run_config))
        object.__setattr__(self, "solve_options", freeze_json(
            _validated_solve_options(self.solve_options)))

        if self.measured_runs != len(self.total_ns_samples):
            raise ValueError("measured_runs disagrees with external samples")
        if self.measured_runs != len(self.resolver_total_ns_samples):
            raise ValueError("measured_runs disagrees with resolver samples")
        for values, label in (
            (self.total_ns_samples, "external benchmark samples"),
            (self.resolver_total_ns_samples, "resolver benchmark samples"),
        ):
            if (not isinstance(values, tuple)
                    or any(isinstance(value, bool)
                           or not isinstance(value, int) or value < 0
                           for value in values)):
                raise ValueError(
                    f"{label} must be non-negative integer tuples")
        if any(external < internal for external, internal in zip(
                self.total_ns_samples, self.resolver_total_ns_samples)):
            raise ValueError(
                "full-call timing cannot be below resolver-reported timing")
        if self.p95_total_ns != _p95(self.total_ns_samples):
            raise ValueError("external p95 disagrees with timing samples")
        if self.p95_resolver_total_ns != _p95(
                self.resolver_total_ns_samples):
            raise ValueError("resolver p95 disagrees with timing samples")
        if self.benchmark_id != strict_hash(self._identity_payload()):
            raise ValueError("planning benchmark identity does not verify")

    @classmethod
    def measure(
            cls,
            resolve: Callable[[], ResolutionOutcome],
            *,
            warmup_runs: int = 1,
            measured_runs: int = 30,
            cache_state: str = "frozen-metadata-warm-process",
            solve_options: MilpSolveOptions | None = None,
            run_config: dict[str, Any] | None = None,
    ) -> "PlanningBenchmarkProfile":
        """Measure a correctness-gated conformance graph in the current process.

        ``solve_options`` records the options used by ``resolve``; callers that
        close over non-default options must pass the same object here. The
        benchmark cannot infer arbitrary state captured by a zero-argument
        callable.
        """
        if (isinstance(warmup_runs, bool) or not isinstance(warmup_runs, int)
                or warmup_runs < 0
                or isinstance(measured_runs, bool)
                or not isinstance(measured_runs, int)
                or measured_runs < 1):
            raise ValueError("benchmark run counts are invalid")
        _required_text(cache_state, "benchmark cache state")
        if solve_options is None:
            solve_options = MilpSolveOptions()
        if not isinstance(solve_options, MilpSolveOptions):
            raise TypeError("solve_options must be MilpSolveOptions")
        declared_run_config = {} if run_config is None else run_config
        if not isinstance(declared_run_config, dict):
            raise TypeError("run_config must be a JSON object")
        declared_run_config = freeze_json(declared_run_config)

        context: tuple[Any, ...] | None = None
        for run_number in range(warmup_runs):
            outcome = resolve()
            current = _validated_outcome_context(
                outcome, f"warmup run {run_number + 1}")
            if context is None:
                context = current
            elif current != context:
                raise ValueError(
                    "benchmark warmup returned changing graph/plan context")

        outcomes: list[ResolutionOutcome] = []
        samples: list[int] = []
        for run_number in range(measured_runs):
            started = perf_counter_ns()
            outcome = resolve()
            elapsed = perf_counter_ns() - started
            current = _validated_outcome_context(
                outcome, f"measured run {run_number + 1}")
            if context is None:
                context = current
            elif current != context:
                raise ValueError(
                    "benchmark resolver returned changing graph/plan context")
            if elapsed < outcome.metrics.total_ns:
                raise ValueError(
                    "external timing did not enclose resolver-reported timing")
            outcomes.append(outcome)
            samples.append(elapsed)

        first = outcomes[0]
        validation = first.validation
        plan = first.selection.plan
        # These are proven non-None by _validated_outcome_context.
        assert validation is not None and plan is not None
        external_samples = tuple(samples)
        resolver_samples = tuple(value.metrics.total_ns for value in outcomes)
        scipy_version = _package_version("scipy")
        values = {
            "benchmark_scope": _BENCHMARK_SCOPE,
            "slo_interpretation": _SLO_INTERPRETATION,
            "timing_source": _TIMING_SOURCE,
            "graph_id": first.hypergraph.graph_id,
            "selector_problem_id": first.selector_problem.problem_id,
            "selection_problem_id": first.selection.selection_problem_id,
            "selected_plan_id": plan.plan_id,
            "validation_report_id": validation.report_id,
            "resolution_status": first.status.value,
            "selection_status": first.selection.status.value,
            "solver_status_code": first.selection.solver_status,
            "discovery_complete": first.hypergraph.discovery_complete,
            "validation_passed": validation.valid,
            "globally_optimal": (
                first.selection.globally_optimal_over_discovery_space),
            "eligible_for_binding": first.eligible_for_binding,
            "requirement_count": len(first.hypergraph.requirement_nodes),
            "use_count": len(first.hypergraph.use_nodes),
            "invocation_count": len(first.hypergraph.invocation_nodes),
            "artifact_count": len(first.hypergraph.artifact_nodes),
            "satisfaction_arc_count": len(
                first.hypergraph.satisfaction_arcs),
            "selected_derivation_depth": _selected_derivation_depth(first),
            "warmup_runs": warmup_runs,
            "measured_runs": measured_runs,
            "cache_state": cache_state,
            "run_config": declared_run_config,
            "solve_options": _solve_options_dict(solve_options),
            "python_version": sys.version.replace("\n", " "),
            "python_implementation": platform_module.python_implementation(),
            "platform": platform_module.platform(),
            "machine_architecture": platform_module.machine() or "unknown",
            "cpu_model": _cpu_model(),
            "logical_cpu_count": os.cpu_count(),
            "memory_total_bytes": _memory_total_bytes(),
            "solver_name": "scipy.optimize.milp",
            "solver_version": scipy_version,
            "highs_version": _highs_version(),
            "scipy_version": scipy_version,
            "numpy_version": _package_version("numpy"),
            "total_ns_samples": external_samples,
            "resolver_total_ns_samples": resolver_samples,
            "p95_total_ns": _p95(external_samples),
            "p95_resolver_total_ns": _p95(resolver_samples),
        }
        return cls(strict_hash(cls._payload(values)), **values)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanningBenchmarkProfile":
        """Strictly deserialize a profile and revalidate its content identity."""
        expected = {"schema", *cls.__dataclass_fields__}
        record = require_object_fields(
            value, expected, "planning benchmark profile")
        if record.pop("schema") != _SCHEMA:
            raise ValueError("unsupported planning benchmark schema")
        for name in ("total_ns_samples", "resolver_total_ns_samples"):
            if not isinstance(record[name], list):
                raise TypeError(f"{name} must be a JSON array")
            record[name] = tuple(record[name])
        return cls(**record)

    @staticmethod
    def _payload(values: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            **strict_copy(values),
        }

    def _identity_payload(self) -> dict[str, Any]:
        return self._payload({
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "benchmark_id"
        })

    def to_dict(self) -> dict[str, Any]:
        return strict_copy({
            "benchmark_id": self.benchmark_id,
            **self._identity_payload(),
        })


def _validated_outcome_context(
        outcome: ResolutionOutcome, label: str,
) -> tuple[Any, ...]:
    if not isinstance(outcome, ResolutionOutcome):
        raise TypeError(f"{label} did not return ResolutionOutcome")
    if outcome.status is not ResolutionStatus.READY:
        raise ValueError(f"{label} did not return READY")
    if not outcome.hypergraph.discovery_complete:
        raise ValueError(f"{label} used an incomplete discovery universe")
    if not outcome.selection.globally_optimal_over_discovery_space:
        raise ValueError(f"{label} was not globally optimal")
    if outcome.selection.plan is None:
        raise ValueError(f"{label} did not return a selected plan")
    if (outcome.validation is None or not outcome.validation.valid
            or not outcome.validation.validation_complete
            or not outcome.validation.candidate_universe_complete):
        raise ValueError(f"{label} did not return complete valid validation")
    if not outcome.eligible_for_binding:
        raise ValueError(f"{label} was not eligible for binding")
    return (
        outcome.hypergraph.graph_id,
        outcome.selector_problem.problem_id,
        outcome.selection.selection_problem_id,
        outcome.selection.plan.plan_id,
        outcome.validation.report_id,
        outcome.status.value,
        outcome.selection.status.value,
        outcome.selection.solver_status,
    )


def _solve_options_dict(options: MilpSolveOptions) -> dict[str, Any]:
    return {
        "time_limit_s": options.time_limit_s,
        "node_limit": options.node_limit,
        "mip_relative_gap": float(options.mip_relative_gap),
        "presolve": options.presolve,
        "display_solver_output": options.display_solver_output,
    }


def _validated_solve_options(value: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "time_limit_s",
        "node_limit",
        "mip_relative_gap",
        "presolve",
        "display_solver_output",
    }
    record = require_object_fields(value, fields, "benchmark solve options")
    options = MilpSolveOptions(**record)
    return _solve_options_dict(options)


def _package_version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "unavailable"


def _highs_version() -> str:
    # SciPy vendors HiGHS and does not install a standalone ``highspy`` package.
    # Import its private version constants lazily and degrade explicitly if a
    # future SciPy layout changes.
    try:
        from scipy.optimize._highspy import _core  # type: ignore[attr-defined]

        return ".".join(str(value) for value in (
            _core.HIGHS_VERSION_MAJOR,
            _core.HIGHS_VERSION_MINOR,
            _core.HIGHS_VERSION_PATCH,
        ))
    except (AttributeError, ImportError):
        return "embedded-version-unavailable"


def _cpu_model() -> str:
    processor = platform_module.processor().strip()
    if processor:
        return processor
    try:
        for line in Path("/proc/cpuinfo").read_text(
                encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() in {
                    "model name", "hardware", "processor"}:
                value = value.strip()
                if value:
                    return value
    except OSError:
        pass
    return platform_module.machine() or "unknown"


def _memory_total_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    if (isinstance(page_size, int) and not isinstance(page_size, bool)
            and isinstance(page_count, int) and not isinstance(page_count, bool)
            and page_size > 0 and page_count > 0):
        return page_size * page_count
    return None


def _selected_derivation_depth(outcome: ResolutionOutcome) -> int:
    plan = outcome.selection.plan
    if plan is None:
        return 0
    owner_by_use = {
        value.use_id: value.owner_invocation_id
        for value in outcome.selector_problem.uses}
    dependencies: dict[str, set[str]] = {
        value: set() for value in plan.selected_invocation_ids}
    for binding in plan.satisfactions:
        owner = owner_by_use[binding.use_id]
        if owner is None:
            continue
        for output in binding.outputs:
            if output.producer_kind.value == "INVOCATION":
                dependencies[owner].add(output.producer_id)
    memo: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(invocation_id: str) -> int:
        if invocation_id in memo:
            return memo[invocation_id]
        if invocation_id in visiting:
            raise ValueError(
                "validated benchmark plan unexpectedly contains a cycle")
        visiting.add(invocation_id)
        value = 1 + max(
            (depth(item) for item in dependencies[invocation_id]), default=0)
        visiting.remove(invocation_id)
        memo[invocation_id] = value
        return value

    return max((depth(value) for value in dependencies), default=0)


__all__ = ["PlanningBenchmarkProfile"]
