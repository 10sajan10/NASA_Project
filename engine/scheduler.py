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
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .backends import Backend, SerialBackend
from .contracts import Request
from .cube_ref import CubeRef, is_cross_process_backend
from .identity import (
    execution_engine_identity,
    git_revision,
    git_worktree_dirty,
    sha256_json,
)
from .log import get_logger
from .pipeline import BoundPipeline, Pipeline, Trigger
from .registry import ProducerRegistry, producer_produces, producer_requires
from .retry import RetryPolicy, attempt_with_retry
from .tiled import _run_tile, is_tile_aware


_log = get_logger(__name__)


# --------------------------------------------------------------- results
@dataclass
class TileMetric:
    """Per-tile execution record for a tile-aware producer."""
    index: int
    elapsed_s: float
    attempts: int
    status: str                                  # "ok" | "error"
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "elapsed_s": self.elapsed_s,
            "attempts": self.attempts,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class StepResult:
    name: str
    status: str                                  # "ok" | "skipped" | "error"
    elapsed_s: float
    produced: dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None
    attempts: int = 1                            # times tried (1..max)
    dead_letter: bool = False                    # ran out of retries
    tile_count: int = 0                          # how many tiles ran
    tile_metrics: list[TileMetric] = field(default_factory=list)

    @property
    def tile_latency_summary(self) -> dict:
        """Min/median/max/total of per-tile elapsed seconds, plus error
        count. Empty dict if there were no tiles."""
        if not self.tile_metrics:
            return {}
        times = sorted(t.elapsed_s for t in self.tile_metrics)
        n = len(times)
        median = times[n // 2] if n % 2 == 1 else (
            (times[n // 2 - 1] + times[n // 2]) / 2.0)
        return {
            "n": n,
            "min_s": times[0],
            "median_s": median,
            "max_s": times[-1],
            "total_s": sum(times),
            "errors": sum(1 for t in self.tile_metrics
                          if t.status == "error"),
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "elapsed_s": self.elapsed_s,
            "produced": dict(self.produced),
            "error": self.error,
            "attempts": self.attempts,
            "dead_letter": self.dead_letter,
            "tile_count": self.tile_count,
            "tile_metrics": [t.to_dict() for t in self.tile_metrics],
            "tile_latency_summary": self.tile_latency_summary,
        }


@dataclass
class RunResult:
    steps: list[StepResult] = field(default_factory=list)
    triggered: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(s.status != "error" for s in self.steps)

    def by_name(self) -> dict[str, StepResult]:
        return {s.name: s for s in self.steps}

    def to_dict(self) -> dict:
        """Serialisable snapshot of the run — drop into JSON for offline
        analysis ("which step failed yesterday at 03:14 UTC?")."""
        return {
            "ok": self.ok,
            "n_steps": len(self.steps),
            "n_errors": sum(1 for s in self.steps if s.status == "error"),
            "n_skipped": sum(1 for s in self.steps if s.status == "skipped"),
            "total_elapsed_s": sum(s.elapsed_s for s in self.steps),
            "triggered": list(self.triggered),
            "steps": [s.to_dict() for s in self.steps],
            "manifest": dict(self.manifest),
        }

    def save_json(self, path) -> None:
        """Write `to_dict()` as JSON. Caller chooses the path; the runner
        passes ``logs/{run_id}.json`` when given a ``run_id``."""
        import json
        from pathlib import Path as _P
        p = _P(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, default=str))


# ------------------------------------------------------------ workers
def _coerce_produced(out: Any) -> dict[str, int]:
    """Normalize a producer's `run` return into {variable: version}.

    Accepts:
      * dict[str, int]    (ProducerV2 default — {variable: version})
      * list[str]         (legacy-shape adapter — names only)
      * None              (producer wrote directly to the cube)
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

      * legacy per-variable: `is_satisfied(cube, variable, request)`
      * ProducerV2 producer-level: `is_satisfied(cube, request)`
      * cube-native fallback: `cube.satisfies(VarSpec, request)` for
        every declared output

    A producer is treated as satisfied only when ALL its declared `produces`
    are satisfied. Any error in the predicate is treated as 'not satisfied'
    so we re-run rather than silently skip.
    """
    if request is not None and getattr(request, "force", False):
        return False
    raw_produces = tuple(getattr(producer, "produces", ()) or ())
    produces = producer_produces(producer)
    if not produces:
        return False

    # Cache-hit decision -----------------------------------------------
    method = getattr(producer, "is_satisfied", None)
    satisfied = False
    if method is not None:
        try:
            # Per-variable signature (legacy shape).
            satisfied = all(method(cube, var, request) for var in produces)
        except TypeError:
            try:
                satisfied = bool(method(cube, request))
            except Exception:
                satisfied = False
        except Exception:
            satisfied = False
    elif hasattr(cube, "satisfies"):
        specs = raw_produces if raw_produces else produces
        try:
            satisfied = all(cube.satisfies(spec, request) for spec in specs)
        except Exception:
            satisfied = False
    else:
        try:
            satisfied = all(cube.has(var) for var in produces)
        except Exception:
            satisfied = False

    if not satisfied:
        return False

    # Dirty propagation: even if the cube has the output, treat it as
    # stale (not satisfied) when any required input has been written
    # AFTER the cached output. This catches the "user re-fetched
    # upstream data; downstream caches are now wrong" case.
    if hasattr(cube, "is_output_stale"):
        requires_names = producer_requires(producer)
        if requires_names:
            try:
                for out in produces:
                    if cube.is_output_stale(out, requires_names):
                        return False
            except Exception:
                # Conservative: if the staleness check itself fails,
                # don't skip — re-run.
                return False

    return True


# ---------------------------------------------------------- the runner
class PipelineRunner:
    """Run a `Pipeline` through a `Backend`."""

    def __init__(self,
                 registry: ProducerRegistry,
                 backend: Optional[Backend] = None,
                 *,
                 verbose: bool = True,
                 fail_fast: bool = False,
                 skip_when_satisfied: bool = True,
                 retry_policy: Optional[RetryPolicy] = None,
                 max_inflight_tiles: Optional[int] = None,
                 result_dir: Optional[Any] = None,
                 allow_runtime_triggers: bool = False) -> None:
        self.registry = registry
        self.backend = backend or SerialBackend()
        self.verbose = verbose
        self.fail_fast = fail_fast
        self.skip_when_satisfied = skip_when_satisfied
        # Default: single attempt (back-compat). Producers + the runner
        # can override per-step via the policy.
        self.retry_policy = retry_policy or RetryPolicy()
        # Backpressure: cap concurrent in-flight tile tasks per producer.
        # None = unbounded (submit all tiles). Useful for grids with
        # thousands of tiles to avoid building up large in-flight result
        # buffers in the parent process.
        self.max_inflight_tiles = max_inflight_tiles
        # Where (if anywhere) to persist RunResult JSON dumps. Default
        # None = don't persist. Set to a Path to drop one file per
        # .run() invocation under that directory.
        self.result_dir = result_dir
        self.allow_runtime_triggers = allow_runtime_triggers

    # ------------------------------------------------------------------
    def run(self,
            cube: Any,
            pipeline: Pipeline | BoundPipeline,
            *,
            t_start=None, t_end=None,
            force: bool = False,
            context: Optional[dict] = None,
            result_path: Optional[Any] = None) -> RunResult:
        """Execute the pipeline. Returns a `RunResult` summarizing each step.

        If `result_path` is given (or `self.result_dir` is set), the
        RunResult is dumped as JSON after the run finishes — including
        when steps fail. Pass `result_path=False` to suppress the dump
        on a per-call basis.
        """
        request = Request(t_start=t_start, t_end=t_end, force=force,
                          context=dict(context or {}))
        bound = (pipeline.bind(
            self.registry,
            allow_runtime_triggers=self.allow_runtime_triggers)
                 if isinstance(pipeline, Pipeline) else pipeline)
        if bound.triggers() and not self.allow_runtime_triggers:
            raise RuntimeError(
                "bound plan contains legacy runtime triggers but the runner "
                "did not opt into them")
        bound.verify_bindings()
        started_at = datetime.now(timezone.utc)
        request_record = {
            "t_start": str(t_start) if t_start is not None else None,
            "t_end": str(t_end) if t_end is not None else None,
            "force": force,
            "context_keys": sorted(str(k) for k in (context or {})),
            "context_sha256": sha256_json(context or {}),
        }
        components = [node.component.to_dict() for node in bound.nodes()]
        result = RunResult(manifest={
            "schema": "stage0-execution-manifest-v1",
            "run_id": str(uuid.uuid4()),
            "started_at": started_at.isoformat(),
            "ended_at": None,
            "status": "running",
            "plan_id": bound.plan_id,
            "pipeline": bound.name,
            "code_revision": git_revision(),
            "code_worktree_dirty": git_worktree_dirty(),
            "execution_engine": execution_engine_identity(),
            "request": request_record,
            "configuration_sha256": sha256_json({
                "request": request_record,
                "components": [c["configuration_sha256"]
                               for c in components],
            }),
            "components": components,
        })
        # Resolve the JSON dump target up front so the try/finally at the
        # bottom of this method can dump even on exceptional exits.
        dump_target = self._resolve_result_path(result_path)

        # working set: each pending node -> its unmet dependencies
        node_after: dict[str, set[str]] = {
            n.name: set(n.after) for n in bound.nodes()
            if not n.trigger_only
        }
        completed: set[str] = set()
        failed: set[str] = set()
        triggers_by_source: dict[str, list[Trigger]] = {}
        for t in bound.triggers():
            triggers_by_source.setdefault(t.source, []).append(t)

        if self.verbose:
            _log.info(bound.explain())
            _log.info(f"[runner] backend={self.backend.name}")

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
                    _log.info(f"[runner] {n} unreachable "
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
            layer_results = self._run_layer(ready, cube, request, bound)
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
                            _log.info(f"[runner] fail_fast: stopping after "
                                  f"{sr.name} error")
                        self._finalize_result(result)
                        self._dump_result(result, dump_target)
                        return result

        self._finalize_result(result)
        self._dump_result(result, dump_target)
        return result

    @staticmethod
    def _finalize_result(result: RunResult) -> None:
        result.manifest["ended_at"] = datetime.now(timezone.utc).isoformat()
        result.manifest["status"] = "succeeded" if result.ok else "failed"

    # ------------------------------------------------------------------
    def _resolve_result_path(self, result_path) -> Optional[Any]:
        """Pick where (if anywhere) to dump the RunResult JSON.

        Precedence: per-call `result_path` overrides the runner's
        `result_dir`. ``result_path=False`` explicitly disables the
        dump. ``result_dir`` (a directory) -> a timestamped filename
        inside it.
        """
        from datetime import datetime as _dt, timezone as _tz
        from pathlib import Path as _P
        if result_path is False:
            return None
        if result_path is not None:
            return _P(result_path)
        if self.result_dir is not None:
            stamp = _dt.now(_tz.utc).strftime("%Y%m%d_%H%M%S")
            return _P(self.result_dir) / f"runresult_{stamp}.json"
        return None

    @staticmethod
    def _dump_result(result: RunResult, path) -> None:
        if path is None:
            return
        try:
            result.save_json(path)
            _log.debug("RunResult JSON written to %s", path)
        except Exception as exc:
            # Never let a logging-layer failure crash the run itself.
            _log.warning("failed to write RunResult JSON to %s: %s",
                         path, exc)

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
                    _log.info(f"[trigger] {trig.name}: predicate error: {e}; "
                          "treating as not fired")
                fired = False
            if not fired:
                continue
            if trig.target in completed:
                if self.verbose:
                    _log.info(f"[trigger] {trig.name}: target {trig.target!r} "
                          "already completed; skipping")
                continue
            # Insert (or merge into) the working set with a dependency on the
            # source so it can't run until source has finished.
            node_after.setdefault(trig.target, set()).add(source)
            result.triggered.append(trig.target)
            if self.verbose:
                _log.info(f"[trigger] {trig.name} fired -> "
                      f"scheduled {trig.target!r}")

    # ------------------------------------------------------------------
    def _run_layer(self, names: list[str], cube,
                   request: Request,
                   pipeline: BoundPipeline) -> list[StepResult]:
        """Submit a layer of independent nodes to the backend."""
        # Serial fast-path: no worker overhead when only one node, or when
        # using SerialBackend explicitly.
        if isinstance(self.backend, SerialBackend) or len(names) == 1:
            return [self._run_step(
                name, pipeline.get(name).producer, cube, request)
                for name in names]

        # Pre-pass: classify nodes into skipped / tile-aware / submit.
        # Tile-aware producers drive their own backend fan-out (one
        # producer's tiles in parallel) and run sequentially within the
        # layer to avoid double-tapping the backend.
        results: list[StepResult] = []
        to_submit: list[str] = []
        for name in names:
            producer = pipeline.get(name).producer
            if self.skip_when_satisfied and _producer_already_satisfied(
                    producer, cube, request):
                if self.verbose:
                    _log.info(f"[step] {name}  skipped (already satisfied)")
                results.append(StepResult(
                    name=name, status="skipped", elapsed_s=0.0))
                continue
            if is_tile_aware(producer):
                results.append(
                    self._run_tiled_step(name, producer, cube, request))
                continue
            to_submit.append(name)

        # Cross-process backends get a picklable CubeRef instead of the
        # live Cube object (DuckDB connections don't pickle reliably).
        worker_cube = (CubeRef.from_cube(cube)
                       if is_cross_process_backend(self.backend)
                       else cube)

        # Parallel via backend.submit so we collect per-task timing/errors.
        # Retries run INSIDE the worker (sleeps don't block the parent),
        # so the future result is a (produced, exc, attempts) tuple.
        futs: list[tuple[str, Future, float]] = []
        for name in to_submit:
            producer = pipeline.get(name).producer
            if self.verbose:
                _log.info(f"[step] {name}  submit ({self.backend.name})")
            t0 = time.monotonic()
            futs.append((name, self.backend.submit(
                attempt_with_retry, _run_one, producer, worker_cube,
                request, policy=self.retry_policy), t0))

        max_attempts = self.retry_policy.max_attempts
        for name, fut, t0 in futs:
            try:
                produced, exc, attempts = fut.result()
            except BaseException as e:
                # Backend-level failure (e.g. worker crash). Treat as
                # immediate dead-letter — we can't retry through a crashed
                # worker safely.
                elapsed = time.monotonic() - t0
                results.append(StepResult(
                    name=name, status="error",
                    elapsed_s=elapsed,
                    error=f"{type(e).__name__}: {e}",
                    attempts=1, dead_letter=True))
                if self.verbose:
                    _log.info(f"[step] {name}  ERROR (backend) after "
                          f"{elapsed:.2f}s: {e}")
                continue
            elapsed = time.monotonic() - t0
            if exc is None:
                tag = f" (after {attempts} attempts)" if attempts > 1 else ""
                results.append(StepResult(
                    name=name, status="ok",
                    elapsed_s=elapsed, produced=produced,
                    attempts=attempts))
                if self.verbose:
                    _log.info(f"[step] {name}  ok in {elapsed:.2f}s{tag} -> "
                          f"{produced}")
            else:
                dead_letter = attempts >= max_attempts
                results.append(StepResult(
                    name=name, status="error",
                    elapsed_s=elapsed,
                    error=f"{type(exc).__name__}: {exc}",
                    attempts=attempts,
                    dead_letter=dead_letter))
                if self.verbose:
                    dtag = " (dead-letter)" if dead_letter else ""
                    _log.info(f"[step] {name}  ERROR after {elapsed:.2f}s "
                          f"({attempts} attempts){dtag}: {exc}")
        return results

    def _run_step(self, name: str, producer, cube,
                  request: Request) -> StepResult:
        if self.skip_when_satisfied and _producer_already_satisfied(
                producer, cube, request):
            if self.verbose:
                _log.info(f"[step] {name}  skipped (already satisfied)")
            return StepResult(name=name, status="skipped", elapsed_s=0.0)

        # Route tile-aware producers through the tile fan-out path. Tiles
        # of a single producer get dispatched to the backend in parallel;
        # producers themselves run one-at-a-time within a layer (already
        # ordered by the DAG).
        if is_tile_aware(producer):
            return self._run_tiled_step(name, producer, cube, request)

        if self.verbose:
            _log.info(f"[step] {name}  start")
        t0 = time.monotonic()
        produced, exc, attempts = attempt_with_retry(
            _run_one, producer, cube, request,
            policy=self.retry_policy)
        elapsed = time.monotonic() - t0
        if exc is None:
            tag = f" (after {attempts} attempts)" if attempts > 1 else ""
            if self.verbose:
                _log.info(f"[step] {name}  ok in {elapsed:.2f}s{tag} -> {produced}")
            return StepResult(name=name, status="ok",
                              elapsed_s=elapsed, produced=produced,
                              attempts=attempts)
        dead_letter = attempts >= self.retry_policy.max_attempts
        if self.verbose:
            tag = f" (dead-letter after {attempts} attempts)" if dead_letter else ""
            _log.info(f"[step] {name}  ERROR after {elapsed:.2f}s{tag}: {exc}")
        return StepResult(name=name, status="error",
                          elapsed_s=elapsed,
                          error=f"{type(exc).__name__}: {exc}",
                          attempts=attempts,
                          dead_letter=dead_letter)

    # ------------------------------------------------------------------
    def _run_tiled_step(self, name: str, producer, cube,
                        request: Request) -> StepResult:
        """Fan one producer's spatial tiles across the backend.

        The producer's `init` runs in the parent (pre-allocates output
        Zarrs), each tile is dispatched as an independent backend task
        that calls `process_tile`, then `finalize` runs in the parent.
        """
        if self.verbose:
            _log.info(f"[step] {name}  start (tile fan-out, "
                  f"backend={self.backend.name})")
        t0 = time.monotonic()
        try:
            producer.init(cube, request)
            all_tiles = list(producer.tile_iter(cube, request))
            # Active-set filter: skip tiles whose predicate is False.
            tiles: list = []
            skipped_inactive = 0
            for t in all_tiles:
                try:
                    active = producer.tile_predicate(cube, request, t)
                except Exception:
                    # A misbehaving predicate must not silently drop work.
                    active = True
                if active:
                    tiles.append(t)
                else:
                    skipped_inactive += 1
            n_tiles = len(tiles)
            if self.verbose:
                msg = f"[step] {name}  {n_tiles} active tiles"
                if skipped_inactive:
                    msg += f" ({skipped_inactive} inactive skipped)"
                _log.info(msg)

            worker_cube = (CubeRef.from_cube(cube)
                           if is_cross_process_backend(self.backend)
                           else cube)

            tile_errors: list[str] = []
            tile_attempts: list[int] = []
            tile_metrics: list[TileMetric] = []

            def _record(i: int, started_at: float, exc, attempts: int,
                        backend_error: bool = False) -> None:
                elapsed = max(0.0, time.monotonic() - started_at)
                tile_attempts.append(attempts)
                if exc is None:
                    tile_metrics.append(TileMetric(
                        index=i, elapsed_s=elapsed,
                        attempts=attempts, status="ok"))
                else:
                    err_text = (f"backend {type(exc).__name__}: {exc}"
                                if backend_error
                                else f"{type(exc).__name__}: {exc}")
                    tile_errors.append(f"tile[{i}] {err_text}")
                    tile_metrics.append(TileMetric(
                        index=i, elapsed_s=elapsed,
                        attempts=attempts, status="error",
                        error=err_text))

            if isinstance(self.backend, SerialBackend) or n_tiles == 1:
                for i, tile in enumerate(tiles):
                    t_tile = time.monotonic()
                    _, exc, attempts = attempt_with_retry(
                        _run_tile, producer, worker_cube, request, tile,
                        policy=self.retry_policy)
                    _record(i, t_tile, exc, attempts)
            else:
                # Bounded sliding window so backpressure caps concurrent
                # in-flight tile futures (memory hygiene at thousands of
                # tiles). Retries run inside workers so the sleep doesn't
                # block the parent's gather.
                cap = self.max_inflight_tiles or n_tiles
                cap = max(1, min(cap, n_tiles))
                pending: dict = {}  # future -> (i, t_start)
                enumerated = list(enumerate(tiles))
                cursor = 0

                def _submit_next() -> None:
                    nonlocal cursor
                    if cursor >= n_tiles:
                        return
                    i, tile = enumerated[cursor]
                    cursor += 1
                    t = time.monotonic()
                    fut = self.backend.submit(
                        attempt_with_retry, _run_tile, producer,
                        worker_cube, request, tile,
                        policy=self.retry_policy)
                    pending[fut] = (i, t)

                for _ in range(cap):
                    _submit_next()

                while pending:
                    # Poll for the first done future. Polling beats
                    # concurrent.futures.wait because Dask futures aren't
                    # always drop-in compatible — but they do implement
                    # done() and result().
                    done = None
                    for fut in list(pending.keys()):
                        if fut.done():
                            done = fut
                            break
                    if done is None:
                        # No one's done yet; block on the oldest.
                        done = next(iter(pending))
                    i, t_start = pending.pop(done)
                    try:
                        _, exc, attempts = done.result()
                        _record(i, t_start, exc, attempts)
                    except BaseException as e:
                        _record(i, t_start, e, 1, backend_error=True)
                    _submit_next()

            max_tile_attempts = (max(tile_attempts) if tile_attempts
                                  else 1)
            if tile_errors:
                # Don't run finalize if tile errors occurred.
                err = ("; ".join(tile_errors[:3])
                       + (f"  (+ {len(tile_errors) - 3} more)"
                          if len(tile_errors) > 3 else ""))
                elapsed = time.monotonic() - t0
                dead_letter = max_tile_attempts >= self.retry_policy.max_attempts
                if self.verbose:
                    dtag = " (some dead-letter)" if dead_letter else ""
                    _log.info(f"[step] {name}  ERROR after {elapsed:.2f}s "
                          f"({len(tile_errors)} tile failures){dtag}")
                return StepResult(name=name, status="error",
                                  elapsed_s=elapsed, error=err,
                                  attempts=max_tile_attempts,
                                  dead_letter=dead_letter,
                                  tile_count=n_tiles,
                                  tile_metrics=tile_metrics)

            produced = producer.finalize(cube, request) or {}
            elapsed = time.monotonic() - t0
            if self.verbose:
                rt = (f" (max {max_tile_attempts} tile attempts)"
                      if max_tile_attempts > 1 else "")
                _log.info(f"[step] {name}  ok in {elapsed:.2f}s "
                      f"({n_tiles} tiles){rt} -> {produced}")
                if tile_metrics:
                    summary_times = sorted(t.elapsed_s for t in tile_metrics)
                    _log.info(f"[step] {name}  tile latency: "
                          f"min={summary_times[0]*1000:.0f}ms "
                          f"median={summary_times[len(summary_times)//2]*1000:.0f}ms "
                          f"max={summary_times[-1]*1000:.0f}ms")
            return StepResult(name=name, status="ok",
                              elapsed_s=elapsed,
                              produced={str(k): int(v)
                                         for k, v in produced.items()},
                              attempts=max_tile_attempts,
                              tile_count=n_tiles,
                              tile_metrics=tile_metrics)
        except BaseException as e:
            elapsed = time.monotonic() - t0
            if self.verbose:
                _log.info(f"[step] {name}  ERROR after {elapsed:.2f}s: {e}")
            return StepResult(name=name, status="error",
                              elapsed_s=elapsed,
                              error=f"{type(e).__name__}: {e}")
