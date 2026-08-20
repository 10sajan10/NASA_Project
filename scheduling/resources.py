"""Resource capacity, reservations, and the no-oversubscription invariant.

The rule this module enforces is blunt: the sum of live reservations never
exceeds declared capacity, on any dimension, ever.  Oversubscribing CPU makes a
benchmark look busy; oversubscribing memory kills the node.  Both are refused
the same way, by refusing to grant the reservation.

Capacity here is *allocation-aware*.  On a shared or cgroup-limited node
``os.cpu_count()`` reports the machine, not the share this process was granted,
and scheduling against the machine is how a well-behaved job becomes a bad
neighbour.  :func:`detect_capacity` reads the cgroup limits when they exist and
falls back to the raw count only when they do not.
"""
from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from capabilities.implementation import _required_text
from engine.runtime.identity import require_object_fields, strict_hash

# Every library that spawns its own thread pool inside our worker process.
# Left uncapped, a 4-core reservation running four tasks becomes 4 x N threads
# fighting over the same cores and everything slows down together.
THREAD_ENVIRONMENT_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)


@dataclass(frozen=True)
class ResourceEnvelopeSpec:
    """What one task asks for, on every dimension the scheduler tracks."""

    cpu_cores: int = 1
    memory_mb: int = 128
    gpus: int = 0
    scratch_mb: int = 0

    def __post_init__(self) -> None:
        # Zero is legitimate here: it is what a fully booked site has free, and
        # what an empty ledger has used.  A *request* for zero is rejected at
        # the reservation boundary instead, where it is actually meaningful.
        for name in ("cpu_cores", "memory_mb", "gpus", "scratch_mb"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    @classmethod
    def zero(cls) -> "ResourceEnvelopeSpec":
        return cls(cpu_cores=0, memory_mb=0, gpus=0, scratch_mb=0)

    @property
    def is_runnable_request(self) -> bool:
        """A real task needs at least one core and some memory."""
        return self.cpu_cores >= 1 and self.memory_mb >= 1

    def fits_within(self, capacity: "ResourceEnvelopeSpec") -> bool:
        return (self.cpu_cores <= capacity.cpu_cores
                and self.memory_mb <= capacity.memory_mb
                and self.gpus <= capacity.gpus
                and self.scratch_mb <= capacity.scratch_mb)

    def plus(self, other: "ResourceEnvelopeSpec") -> "ResourceEnvelopeSpec":
        return ResourceEnvelopeSpec(
            self.cpu_cores + other.cpu_cores,
            self.memory_mb + other.memory_mb,
            self.gpus + other.gpus,
            self.scratch_mb + other.scratch_mb)

    def minus(self, other: "ResourceEnvelopeSpec") -> "ResourceEnvelopeSpec":
        return ResourceEnvelopeSpec(
            max(self.cpu_cores - other.cpu_cores, 0),
            max(self.memory_mb - other.memory_mb, 0),
            max(self.gpus - other.gpus, 0),
            max(self.scratch_mb - other.scratch_mb, 0))

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResourceEnvelopeSpec":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ResourceEnvelopeSpec")
        return cls(**raw)


@dataclass(frozen=True)
class ExecutionSite:
    """One place work can run, with its capacity and environment classes.

    ``environment_classes`` drives affinity: a task that declares a needed
    environment may only be placed where that environment exists, and among
    otherwise-equal sites the scheduler prefers one that already has it.
    """

    site_id: str
    capacity: ResourceEnvelopeSpec
    environment_classes: tuple[str, ...] = ()
    network_classes: tuple[str, ...] = ()
    host_id: str | None = None
    host_capacity: ResourceEnvelopeSpec | None = None

    def __post_init__(self) -> None:
        _required_text(self.site_id, "site_id")
        # Logical sites can share physical hardware.  Without a host the
        # ledger can only promise that each *logical* site is within its own
        # declared capacity, which says nothing about the machine.
        if self.host_id is not None:
            _required_text(self.host_id, "host_id")
            if not isinstance(self.host_capacity, ResourceEnvelopeSpec):
                raise TypeError(
                    "a site naming a host must declare that host's capacity")
        elif self.host_capacity is not None:
            raise ValueError("host capacity requires a host_id")
        if not isinstance(self.capacity, ResourceEnvelopeSpec):
            raise TypeError("site capacity must be a ResourceEnvelopeSpec")
        for values, label in ((self.environment_classes, "environment classes"),
                              (self.network_classes, "network classes")):
            if (not isinstance(values, tuple)
                    or any(not isinstance(item, str) or not item
                           for item in values)):
                raise TypeError(f"{label} must be a text tuple")
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{label} must be unique and sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "capacity": self.capacity.to_dict(),
            "environment_classes": list(self.environment_classes),
            "network_classes": list(self.network_classes),
            "host_id": self.host_id,
            "host_capacity": (self.host_capacity.to_dict()
                              if self.host_capacity is not None else None),
        }


class OversubscriptionError(RuntimeError):
    """A reservation would exceed declared capacity on some dimension."""

    def __init__(self, site_id: str, dimension: str, requested: int,
                 available: int) -> None:
        super().__init__(
            f"site {site_id!r} cannot grant {requested} {dimension}; "
            f"{available} available")
        self.site_id = site_id
        self.dimension = dimension
        self.requested = requested
        self.available = available


@dataclass(frozen=True)
class Reservation:
    """One granted claim on a site, released by an explicit policy event."""

    reservation_id: str
    task_key: str
    site_id: str
    envelope: ResourceEnvelopeSpec

    def __post_init__(self) -> None:
        for value, label in ((self.reservation_id, "reservation_id"),
                             (self.task_key, "reservation task_key"),
                             (self.site_id, "reservation site_id")):
            _required_text(value, label)
        if not isinstance(self.envelope, ResourceEnvelopeSpec):
            raise TypeError("reservation envelope is invalid")

    @classmethod
    def bind(cls, task_key: str, site_id: str,
             envelope: ResourceEnvelopeSpec) -> "Reservation":
        return cls(
            strict_hash({
                "schema": "stage8-reservation-v1",
                "task_key": task_key, "site_id": site_id,
                "envelope": envelope.to_dict(),
            }), task_key, site_id, envelope)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "task_key": self.task_key,
            "site_id": self.site_id,
            "envelope": self.envelope.to_dict(),
        }


class ReservationLedger:
    """Tracks live reservations per site and refuses to oversubscribe."""

    def __init__(self, sites: Iterable[ExecutionSite]) -> None:
        self._sites = {site.site_id: site for site in sites}
        if not self._sites:
            raise ValueError("a ledger needs at least one execution site")
        self._live: dict[str, Reservation] = {}
        self._host_capacity: dict[str, ResourceEnvelopeSpec] = {}
        for site in self._sites.values():
            if site.host_id is None:
                continue
            assert site.host_capacity is not None
            known = self._host_capacity.setdefault(
                site.host_id, site.host_capacity)
            if known != site.host_capacity:
                raise ValueError(
                    f"sites disagree about the capacity of host "
                    f"{site.host_id!r}")

    def host_used(self, host_id: str) -> ResourceEnvelopeSpec:
        total = ResourceEnvelopeSpec.zero()
        for reservation in self._live.values():
            site = self._sites[reservation.site_id]
            if site.host_id == host_id:
                total = total.plus(reservation.envelope)
        return total

    @property
    def sites(self) -> tuple[ExecutionSite, ...]:
        return tuple(self._sites[key] for key in sorted(self._sites))

    def site(self, site_id: str) -> ExecutionSite:
        try:
            return self._sites[site_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution site {site_id!r}") from exc

    def used(self, site_id: str) -> ResourceEnvelopeSpec:
        total = ResourceEnvelopeSpec.zero()
        for reservation in self._live.values():
            if reservation.site_id == site_id:
                total = total.plus(reservation.envelope)
        return total

    def available(self, site_id: str) -> ResourceEnvelopeSpec:
        return self.site(site_id).capacity.minus(self.used(site_id))

    def can_fit(self, site_id: str, envelope: ResourceEnvelopeSpec) -> bool:
        site = self.site(site_id)
        if not envelope.fits_within(self.available(site_id)):
            return False
        # Placement is a promise that reserve() can immediately keep.  A
        # per-site-only answer is false when several logical sites share one
        # physical host whose aggregate capacity is already exhausted.
        if site.host_id is not None:
            assert site.host_capacity is not None
            host_free = site.host_capacity.minus(self.host_used(site.host_id))
            if not envelope.fits_within(host_free):
                return False
        return True

    def reserve(self, task_key: str, site_id: str,
                envelope: ResourceEnvelopeSpec) -> Reservation:
        """Grant a reservation, or refuse with the dimension that blocked it."""
        if task_key in self._live:
            raise ValueError(f"task {task_key!r} already holds a reservation")
        if not envelope.is_runnable_request:
            raise ValueError(
                "a reservation needs at least one core and some memory")
        site = self.site(site_id)
        dimensions = ("cpu_cores", "memory_mb", "gpus", "scratch_mb")
        # Report the dimension that actually blocked, whether the request
        # exceeds the site outright or merely what is free right now. Naming
        # the wrong dimension sends a reader hunting the wrong resource.
        for dimension in dimensions:
            requested = getattr(envelope, dimension)
            if requested > getattr(site.capacity, dimension):
                raise OversubscriptionError(
                    site_id, dimension, requested,
                    getattr(site.capacity, dimension))
        free = self.available(site_id)
        for dimension in dimensions:
            requested = getattr(envelope, dimension)
            if requested > getattr(free, dimension):
                raise OversubscriptionError(
                    site_id, dimension, requested, getattr(free, dimension))
        # Two logical sites can map onto the same physical CPUs; a per-site
        # check alone would let them jointly overrun the machine.
        if site.host_id is not None:
            assert site.host_capacity is not None
            host_free = site.host_capacity.minus(self.host_used(site.host_id))
            for dimension in dimensions:
                requested = getattr(envelope, dimension)
                if requested > getattr(host_free, dimension):
                    raise OversubscriptionError(
                        site.host_id, dimension, requested,
                        getattr(host_free, dimension))
        reservation = Reservation.bind(task_key, site_id, envelope)
        self._live[task_key] = reservation
        return reservation

    def release(self, task_key: str) -> Reservation:
        try:
            return self._live.pop(task_key)
        except KeyError as exc:
            raise KeyError(
                f"task {task_key!r} holds no reservation to release") from exc

    def reservation(self, owner_id: str) -> Reservation | None:
        """Return the live claim for one task/attempt owner, if present."""
        return self._live.get(owner_id)

    def rekey(self, owner_id: str, new_owner_id: str) -> Reservation:
        """Move an already-granted claim to a durable attempt identity.

        Dispatch must reserve before it creates an attempt.  Once the store
        mints the attempt/fencing identity, this operation changes only the
        ledger key: usage never drops to zero and is never counted twice.
        """
        if owner_id == new_owner_id:
            try:
                return self._live[owner_id]
            except KeyError as exc:
                raise KeyError(
                    f"task {owner_id!r} holds no reservation to rekey") from exc
        if new_owner_id in self._live:
            raise ValueError(
                f"task {new_owner_id!r} already holds a reservation")
        try:
            previous = self._live.pop(owner_id)
        except KeyError as exc:
            raise KeyError(
                f"task {owner_id!r} holds no reservation to rekey") from exc
        replacement = Reservation.bind(
            new_owner_id, previous.site_id, previous.envelope)
        self._live[new_owner_id] = replacement
        return replacement

    def live_task_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._live))

    def invariant_holds(self) -> bool:
        """Live usage is within every declared site *and host* capacity."""
        if not all(self.used(site.site_id).fits_within(site.capacity)
                   for site in self.sites):
            return False
        return all(
            self.host_used(host_id).fits_within(capacity)
            for host_id, capacity in self._host_capacity.items())


def best_fit_site(ledger: ReservationLedger, envelope: ResourceEnvelopeSpec, *,
                  required_environments: tuple[str, ...] = (),
                  required_networks: tuple[str, ...] = ()) -> str | None:
    """Choose the feasible site that leaves the least slack.

    Best fit rather than first fit, so a large task is not blocked by a small
    one scattered across every site.  Ties break on affinity (prefer a site that
    already carries the needed environments) and then on site ID for
    determinism.
    """
    candidates = []
    for site in ledger.sites:
        if not set(required_environments).issubset(site.environment_classes):
            continue
        if not set(required_networks).issubset(site.network_classes):
            continue
        if not ledger.can_fit(site.site_id, envelope):
            continue
        free = ledger.available(site.site_id)
        # Scarce accelerators and scratch dominate: consuming the only GPU
        # node for CPU-only work is how a feasible schedule becomes a bad one.
        # Sites are ranked on the resources the task does *not* need first, so
        # scarce capacity is preserved for work that does need it.
        scarcity = (free.gpus - envelope.gpus,
                    free.scratch_mb - envelope.scratch_mb)
        slack = (free.cpu_cores - envelope.cpu_cores,
                 free.memory_mb - envelope.memory_mb)
        affinity = 0 if required_environments else len(site.environment_classes)
        candidates.append((scarcity[0], scarcity[1], slack[0], slack[1],
                           affinity, site.site_id))
    if not candidates:
        return None
    return min(candidates)[-1]


def thread_environment(envelope: ResourceEnvelopeSpec,
                       base: dict[str, str] | None = None) -> dict[str, str]:
    """Cap nested BLAS/OpenMP threads to the cores actually reserved.

    Without this a task reserving one core still starts a thread per machine
    core inside NumPy, and several such tasks oversubscribe the node even
    though the reservation ledger says everything fits.
    """
    environment = dict(base or {})
    for name in THREAD_ENVIRONMENT_VARIABLES:
        environment[name] = str(envelope.cpu_cores)
    return environment


def _cgroup_cpu_quota() -> int | None:
    """Cores granted by cgroup v2 or v1, or ``None`` when unrestricted."""
    v2 = Path("/sys/fs/cgroup/cpu.max")
    try:
        if v2.exists():
            quota, period = v2.read_text().split()
            if quota != "max":
                return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        return None
    try:
        quota_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period_path = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota_path.exists() and period_path.exists():
            quota = int(quota_path.read_text().strip())
            period = int(period_path.read_text().strip())
            if quota > 0 and period > 0:
                return max(1, quota // period)
    except (OSError, ValueError):
        return None
    return None


def detect_capacity(*, memory_mb: int | None = None,
                    scratch_mb: int = 0) -> ResourceEnvelopeSpec:
    """Allocation-aware capacity, not raw machine size.

    Prefers the cgroup CPU quota and the process affinity mask over
    ``os.cpu_count()``; on a shared node those differ, and the machine total is
    the wrong number to schedule against.
    """
    quota = _cgroup_cpu_quota()
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = os.cpu_count() or 1
    cores = min(value for value in (quota, affinity) if value)
    if memory_mb is None:
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            physical_bytes = pages * page_size
            # Keep the generic scheduling helper aligned with the live
            # SiteSnapshot: cgroup-v2 memory.max is an allocation boundary,
            # not an advisory figure.
            from engine.runtime.site import _cgroup_memory_limit_bytes
            cgroup_bytes = _cgroup_memory_limit_bytes(
                physical_bytes=physical_bytes)
            effective = (physical_bytes if cgroup_bytes is None
                         else min(physical_bytes, cgroup_bytes))
            memory_mb = max(1, effective // (1024 * 1024))
        except (AttributeError, OSError, ValueError):
            memory_mb = 1024
    return ResourceEnvelopeSpec(cpu_cores=max(1, cores), memory_mb=memory_mb,
                                gpus=0, scratch_mb=scratch_mb)


__all__ = [
    "THREAD_ENVIRONMENT_VARIABLES",
    "ExecutionSite",
    "OversubscriptionError",
    "Reservation",
    "ReservationLedger",
    "ResourceEnvelopeSpec",
    "best_fit_site",
    "detect_capacity",
    "thread_environment",
]
