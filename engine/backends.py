"""Execution backends.

The same producer code runs unchanged across:

  * Serial   - single-thread, in-process; debugging & small grids
  * Thread   - single-process, multi-thread; I/O-bound work (Zarr/network)
  * Process  - single-node, multi-process; CPU-bound (numpy/numerics)
  * Dask     - LocalCluster or attach to existing scheduler
  * SLURM    - dask-jobqueue.SLURMCluster for HPC partitions

Backends share a tiny `submit / map / shutdown` surface so the scheduler
(Phase 2) can swap them without code changes. Imports for dask /
dask-jobqueue are deferred so this module stays cheap on minimal installs.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from concurrent.futures import (Future, ProcessPoolExecutor,
                                ThreadPoolExecutor)
from typing import Any, Callable, Iterable, Optional


class Backend(ABC):
    """Minimal execution surface used by the scheduler."""

    name: str = "abstract"

    @abstractmethod
    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> Future:
        """Schedule one task. Returns a concurrent.futures-like Future."""

    @abstractmethod
    def map(self, fn: Callable[[Any], Any],
            iterable: Iterable[Any]) -> Iterable[Any]:
        """Map fn over iterable. Order-preserving for predictable merges."""

    @abstractmethod
    def shutdown(self, wait: bool = True) -> None:
        """Release pool / cluster. Safe to call repeatedly."""

    # Context-manager support for `with make_backend(...) as b:`.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown(wait=True)


class SerialBackend(Backend):
    """Run tasks immediately in the calling thread.

    Useful when n_tiles is small (worker-spawn cost > tile cost), when
    debugging (full traceback in-process), or as a fallback if the parallel
    backend fails to start.
    """
    name = "serial"

    def submit(self, fn, *args, **kwargs):
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # propagate as Future exception
            f.set_exception(e)
        return f

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def shutdown(self, wait: bool = True) -> None:
        pass


class ThreadBackend(Backend):
    """ThreadPoolExecutor. Best for I/O-bound work (Zarr writes, GCS reads)."""
    name = "thread"

    def __init__(self, max_workers: Optional[int] = None):
        self._ex = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, fn, *args, **kwargs):
        return self._ex.submit(fn, *args, **kwargs)

    def map(self, fn, iterable):
        return list(self._ex.map(fn, iterable))

    def shutdown(self, wait: bool = True) -> None:
        self._ex.shutdown(wait=wait)


class ProcessBackend(Backend):
    """ProcessPoolExecutor. Best for CPU-bound numpy work that releases GIL
    only sporadically. Each worker pays import cost once at startup, so
    callers should reuse the backend across producers when possible."""
    name = "process"

    def __init__(self, max_workers: Optional[int] = None):
        # Bound default to avoid oversubscribing on shared HPC nodes.
        if max_workers is None:
            max_workers = max(1, (os.cpu_count() or 1) - 1)
        self._ex = ProcessPoolExecutor(max_workers=max_workers)

    def submit(self, fn, *args, **kwargs):
        return self._ex.submit(fn, *args, **kwargs)

    def map(self, fn, iterable):
        return list(self._ex.map(fn, iterable))

    def shutdown(self, wait: bool = True) -> None:
        self._ex.shutdown(wait=wait)


class DaskBackend(Backend):
    """Dask-backed execution for local clusters or distributed setups.

    address:
      None | "local"   -> dask.distributed.LocalCluster
      "slurm"          -> dask_jobqueue.SLURMCluster (HPC)
      "tcp://host:port"-> attach to an existing scheduler

    n_workers: int. For "slurm" this is the number of SLURM jobs to scale to.
    cluster_kwargs: forwarded to LocalCluster / SLURMCluster.
    client_kwargs:  forwarded to dask.distributed.Client.
    """
    name = "dask"

    def __init__(self,
                 address: Optional[str] = None,
                 n_workers: Optional[int] = None,
                 cluster_kwargs: Optional[dict] = None,
                 client_kwargs: Optional[dict] = None):
        # Deferred imports so non-dask environments don't pay the cost.
        from dask.distributed import Client

        cluster_kwargs = dict(cluster_kwargs or {})
        client_kwargs = dict(client_kwargs or {})

        self._cluster = None
        if address in (None, "local"):
            from dask.distributed import LocalCluster
            self._cluster = LocalCluster(
                n_workers=n_workers if n_workers is not None
                else max(1, (os.cpu_count() or 1) - 1),
                processes=True,
                **cluster_kwargs,
            )
            self._client = Client(self._cluster, **client_kwargs)
            self.name = "dask-local"
        elif address == "slurm":
            try:
                from dask_jobqueue import SLURMCluster
            except ImportError as exc:
                raise ImportError(
                    "address='slurm' requires `pip install dask-jobqueue`"
                ) from exc
            self._cluster = SLURMCluster(**cluster_kwargs)
            self._cluster.scale(jobs=n_workers if n_workers is not None else 4)
            self._client = Client(self._cluster, **client_kwargs)
            self.name = "dask-slurm"
        else:
            self._client = Client(address, **client_kwargs)
            self.name = f"dask:{address}"

    def submit(self, fn, *args, **kwargs):
        return self._client.submit(fn, *args, **kwargs)

    def map(self, fn, iterable):
        items = list(iterable)
        futs = self._client.map(fn, items)
        return self._client.gather(futs)

    def shutdown(self, wait: bool = True) -> None:
        try:
            self._client.close()
        finally:
            if self._cluster is not None:
                self._cluster.close()


def make_backend(mode: str = "serial", **kwargs) -> Backend:
    """Factory: pick a backend by name.

      mode = "serial"  -> SerialBackend
      mode = "thread"  -> ThreadBackend(max_workers=...)
      mode = "process" -> ProcessBackend(max_workers=...)
      mode = "dask"    -> DaskBackend(address="local", ...)
      mode = "slurm"   -> DaskBackend(address="slurm", ...) [HPC]
    """
    mode = (mode or "serial").lower()
    if mode == "serial":
        return SerialBackend()
    if mode == "thread":
        return ThreadBackend(**kwargs)
    if mode == "process":
        return ProcessBackend(**kwargs)
    if mode == "dask":
        return DaskBackend(**kwargs)
    if mode == "slurm":
        return DaskBackend(address="slurm", **kwargs)
    raise ValueError(
        f"unknown backend mode {mode!r}; "
        "expected one of: serial, thread, process, dask, slurm")
