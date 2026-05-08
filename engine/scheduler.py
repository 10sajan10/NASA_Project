"""PipelineRunner: drive a Pipeline through a Backend.

Walks the DAG in topological layers; nodes within a layer run concurrently
through the configured Backend. Triggers are evaluated after each completed
node and may inject new work into the pending set, making the DAG *dynamic*
within a single run.

The runner is intentionally model-agnostic. It only knows how to:

  1. Look up a producer by name from a `ProducerRegistry`.
  2. Call `producer.run(cube, request)` (the only contract requirement).
  3. Record results, fire triggers, propagate readiness to dependents.

That keeps the orchestration substrate decoupled from any specific model,
adapter, or variable schema.

Backend caveats:
  * SerialBackend / ThreadBackend share the cube object directly.
  * ProcessBackend / DaskBackend require the cube + producer to be
    picklable, OR the producer's `run` to reopen the cube from a path.
    The default Cube uses Zarr+DuckDB; some connections aren't picklable.
    For Phase 2, default to Serial/Thread for safety. Phase 3 introduces
    cube-by-path producers that work cross-process.
"""
from __future__ import annotations

import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Optional

from .backends import Backend, SerialBackend
from .contracts import Request
from .cube_ref import CubeRef, is_cross_process_backend
from .pipeline import Pipeline, Trigger
from .registry import ProducerRegistry, producer_produces


# --------------------------------------------------------------- results
@dataclass
class StepResult:
    name: str
    status: str                                  # "ok" | "skipped" | "error"
    elapsed_s: float
    produced: dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class RunResult:
    steps: list[StepResult] = field(default_factory=list)
    triggered: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status != "error" for s in self.steps)

    def by_name(self) -> dict[str, StepResult]:
        return {s.name: s for s in self.steps}


# ------------------------------------------------------------ workers
def _coerce_produced(out: Any) -> dict[str, int]:
    """Normalize a producer's `run` return into {variable: version}.

    Accepts:
      * dict[str, int]    (ProducerV2 default)
      * list[str]         (legacy fusion adapter)
      * None              (driver wrote directly to the cube)
    """
    if out is None:
        return {}
    if isinstance(out, dict):
        return {str(k): int(v) for k, v in out.items()}
    if isinstance(out, list):
        return {str(name): 0 for name in out}
    if isinstance(out, str):
        return {out: 0}
    return {}


def _run_one(producer, cube_or_ref, request: Request) -> dict[str, int]:
    """Worker entry: call the producer, normalize its return shape.

    Accepts either a live Cube (in-process backends) or a CubeRef
    (cross-process backends — worker reopens the cube here). Either way
    the producer sees a real cube object."""
    if isinstance(cube_or_ref, CubeRef):
        with cube_or_ref.opened() as cube:
            return _coerce_produced(producer.run(cube, request))
    return _coerce_produced(producer.run(cube_or_ref, request))


def _producer_already_satisfied(producer, cube, request: Request) -> bool:
    """Best-effort 'is the cube already in the desired state for this producer?'.

    Honors two contract shapes without coupling to either:

      * legacy fusion: `is_satisfied(cube, variable, request)` - per-variable
      * engine v2 (future): `is_satisfied(cube, request)` - producer-level

    A producer is treated as satisfied only when ALL its declared `produces`
    are satisfied. Any error in the predicate is treated as 'not satisfied'
    so we re-run rather than silently skip.

    Returns False when the producer doesn't expose `is_satisfied` at all.
    """
    method = getattr(producer, "is_satisfied", None)
    if method is None:
        return False
    if request is not None and getattr(request, "force", False):
        return False
    produces = producer_produces(producer)
    if not produces:
        return False
    try:
        # Try the per-variable signature first (legacy).
        return all(method(cube, var, request) for var in produces)
    except TypeError:
        try:
            # Producer-level signature.
            return bool(method(cube, request))
        except Exception:
            return False
    except Exception:
        return False


# ---------------------------------------------------------- the runner
class PipelineRunner:
    """Run a `Pipeline` through a `Backend`."""

    def __init__(self,
                 registry: ProducerRegistry,
                 backend: Optional[Backend] = None,
                 *,
                 verbose: bool = True,
                 fail_fast: bool = False,
                 skip_when_satisfied: bool = True) -> None:
        self.registry = registry
        self.backend = backend or SerialBackend()
        self.verbose = verbose
        self.fail_fast = fail_fast
        self.skip_when_satisfied = skip_when_satisfied

    # ------------------------------------------------------------------
    def run(self,
            cube: Any,
            pipeline: Pipeline,
            *,
            t_start=None, t_end=None,
            force: bool = False,
            context: Optional[dict] = None) -> RunResult:
        """Execute the pipeline. Returns a `RunResult` summarizing each step."""
        request = Request(t_start=t_start, t_end=t_end, force=force,
                          context=dict(context or {}))
        result = RunResult()

        # working set: each pending node -> its unmet dependencies
        node_after: dict[str, set[str]] = {
            n.name: set(n.after) for n in pipeline.nodes()
        }
        completed: set[str] = set()
        failed: set[str] = set()
        triggers_by_source: dict[str, list[Trigger]] = {}
        for t in pipeline.triggers():
            triggers_by_source.setdefault(t.source, []).append(t)

        if self.verbose:
            print(pipeline.explain())
            print(f"[runner] backend={self.backend.name}")

        while node_after:
            # nodes whose deps are all completed (deps that failed: the node
            # is unreachable; we warn and drop it)
            ready: list[str] = []
            unreachable: list[str] = []
            for n, deps in node_after.items():
                pending_deps = deps - completed - failed
                if pending_deps:
                    continue
                blocked = deps & failed
                if blocked:
                    unreachable.append(n)
                else:
                    ready.append(n)

            for n in unreachable:
                if self.verbose:
                    print(f"[runner] {n} unreachable "
                          f"(failed deps: {sorted(node_after[n] & failed)})")
                node_after.pop(n, None)
                result.steps.append(StepResult(
                    name=n, status="error", elapsed_s=0.0,
                    error="upstream dependency failed"))
                failed.add(n)

            if not ready:
                if not node_after:
                    break
                stuck = ", ".join(sorted(node_after.keys()))
                raise RuntimeError(
                    f"pipeline stuck (no ready nodes; remaining: {stuck})")

            ready.sort()
            layer_results = self._run_layer(ready, cube, request)
            result.steps.extend(layer_results)

            for sr in layer_results:
                node_after.pop(sr.name, None)
                if sr.status == "ok":
                    completed.add(sr.name)
                    self._fire_triggers(sr.name, cube,
                                        triggers_by_source.get(sr.name, ()),
                                        node_after, completed, result)
                elif sr.status == "skipped":
                    # already-satisfied: dependents can proceed; triggers do
                    # NOT fire (the source step didn't actually run).
                    completed.add(sr.name)
                else:
                    failed.add(sr.name)
                    if self.fail_fast:
                        if self.verbose:
                            print(f"[runner] fail_fast: stopping after "
                                  f"{sr.name} error")
                        return result

        return result

    # ------------------------------------------------------------------
    def _fire_triggers(self,
                       source: str,
                       cube: Any,
                       triggers: list[Trigger],
                       node_after: dict[str, set[str]],
                       completed: set[str],
                       result: RunResult) -> None:
        for trig in triggers:
            try:
                fired = bool(trig.when(cube))
            except Exception as e:
                if self.verbose:
                    print(f"[trigger] {trig.name}: predicate error: {e}; "
                          "treating as not fired")
                fired = False
            if not fired:
                continue
            if trig.target in completed:
                if self.verbose:
                    print(f"[trigger] {trig.name}: target {trig.target!r} "
                          "already completed; skipping")
                continue
            # Insert (or merge into) the working set with a dependency on the
            # source so it can't run until source has finished.
            node_after.setdefault(trig.target, set()).add(source)
            result.triggered.append(trig.target)
            if self.verbose:
                print(f"[trigger] {trig.name} fired -> "
                      f"scheduled {trig.target!r}")

    # ------------------------------------------------------------------
    def _run_layer(self, names: list[str], cube,
                   request: Request) -> list[StepResult]:
        """Submit a layer of independent nodes to the backend."""
        # Serial fast-path: no worker overhead when only one node, or when
        # using SerialBackend explicitly.
        if isinstance(self.backend, SerialBackend) or len(names) == 1:
            return [self._run_step(name, cube, request) for name in names]

        # Pre-pass: mark satisfied nodes as skipped without submitting.
        results: list[StepResult] = []
        to_submit: list[str] = []
        for name in names:
            producer = self.registry.get(name)
            if self.skip_when_satisfied and _producer_already_satisfied(
                    producer, cube, request):
                if self.verbose:
                    print(f"[step] {name}  skipped (already satisfied)")
                results.append(StepResult(
                    name=name, status="skipped", elapsed_s=0.0))
                continue
            to_submit.append(name)

        # Cross-process backends get a picklable CubeRef instead of the
        # live Cube object (DuckDB connections don't pickle reliably).
        worker_cube = (CubeRef.from_cube(cube)
                       if is_cross_process_backend(self.backend)
                       else cube)

        # Parallel via backend.submit so we collect per-task timing/errors.
        futs: list[tuple[str, Future, float]] = []
        for name in to_submit:
            producer = self.registry.get(name)
            if self.verbose:
                print(f"[step] {name}  submit ({self.backend.name})")
            t0 = time.monotonic()
            futs.append((name, self.backend.submit(
                _run_one, producer, worker_cube, request), t0))

        for name, fut, t0 in futs:
            try:
                produced = fut.result()
                elapsed = time.monotonic() - t0
                results.append(StepResult(
                    name=name, status="ok",
                    elapsed_s=elapsed, produced=produced))
                if self.verbose:
                    print(f"[step] {name}  ok in {elapsed:.2f}s -> {produced}")
            except BaseException as e:
                elapsed = time.monotonic() - t0
                results.append(StepResult(
                    name=name, status="error",
                    elapsed_s=elapsed,
                    error=f"{type(e).__name__}: {e}"))
                if self.verbose:
                    print(f"[step] {name}  ERROR after {elapsed:.2f}s: {e}")
        return results

    def _run_step(self, name: str, cube, request: Request) -> StepResult:
        producer = self.registry.get(name)
        if self.skip_when_satisfied and _producer_already_satisfied(
                producer, cube, request):
            if self.verbose:
                print(f"[step] {name}  skipped (already satisfied)")
            return StepResult(name=name, status="skipped", elapsed_s=0.0)
        if self.verbose:
            print(f"[step] {name}  start")
        t0 = time.monotonic()
        try:
            produced = _run_one(producer, cube, request)
            elapsed = time.monotonic() - t0
            if self.verbose:
                print(f"[step] {name}  ok in {elapsed:.2f}s -> {produced}")
            return StepResult(name=name, status="ok",
                              elapsed_s=elapsed, produced=produced)
        except BaseException as e:
            elapsed = time.monotonic() - t0
            if self.verbose:
                print(f"[step] {name}  ERROR after {elapsed:.2f}s: {e}")
            return StepResult(name=name, status="error",
                              elapsed_s=elapsed,
                              error=f"{type(e).__name__}: {e}")
