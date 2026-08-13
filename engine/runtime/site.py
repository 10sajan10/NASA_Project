"""Private-node execution-site discovery and conservative preflight."""
from __future__ import annotations

import os
import socket
import time
from pathlib import Path

from .types import ResourceRequest, SiteSnapshot


_REMOTE_FILESYSTEMS = {
    "nfs", "nfs4", "lustre", "cifs", "smbfs", "fuse.sshfs", "ceph",
    "cephfs", "gpfs", "panfs", "glusterfs", "afs", "9p",
}


def filesystem_type(path: Path) -> tuple[str, str]:
    resolved = path.resolve()
    best: tuple[int, str, str] | None = None
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        post = right.split()
        if len(fields) < 5 or not post:
            continue
        mount = fields[4].replace("\\040", " ")
        try:
            resolved.relative_to(mount)
        except ValueError:
            continue
        candidate = (len(mount), post[0], mount)
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise RuntimeError(f"could not identify filesystem for {resolved}")
    return best[1], best[2]


def validate_runtime_root(root: Path) -> tuple[Path, str]:
    if not root.is_absolute():
        raise ValueError("runtime root must be an explicit absolute path")
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    fs_type, _mount = filesystem_type(resolved)
    normalized = fs_type.lower()
    if normalized in _REMOTE_FILESYSTEMS or normalized.startswith("fuse."):
        raise ValueError(
            f"runtime control state requires node-local POSIX storage; "
            f"{resolved} is {fs_type}")
    probe = resolved / ".stage1-write-probe"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write("ok")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        probe.unlink(missing_ok=True)
    directory_fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return resolved, fs_type


def current_private_site(*, memory_limit_mb: int | None = None,
                         walltime_limit_s: float | None = None) -> SiteSnapshot:
    cpuset = tuple(sorted(os.sched_getaffinity(0)))
    pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    physical_mb = int(pages * page_size / (1024 * 1024))
    cgroup_limit = _cgroup_v1_memory_limit_bytes()
    detected_memory_mb = min(
        physical_mb,
        int(cgroup_limit / (1024 * 1024)) if cgroup_limit else physical_mb,
    )
    if memory_limit_mb is None:
        memory_limit_mb = detected_memory_mb
    else:
        if memory_limit_mb <= 0:
            raise ValueError("memory_limit_mb must be positive")
        memory_limit_mb = min(memory_limit_mb, detected_memory_mb)

    visible_raw = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible_raw is None:
        visible_raw = os.environ.get("NVIDIA_VISIBLE_DEVICES")
    if visible_raw is not None:
        visible = visible_raw.strip()
        if visible.lower() in {"", "-1", "none", "void"}:
            gpu_ids = ()
        elif visible.lower() == "all":
            gpu_ids = _device_gpu_ids()
        else:
            gpu_ids = tuple(v.strip() for v in visible.split(",") if v.strip())
    else:
        gpu_ids = _device_gpu_ids()
    expiry = None
    if walltime_limit_s is not None:
        if walltime_limit_s <= 0:
            raise ValueError("walltime_limit_s must be positive")
        expiry = time.time() + walltime_limit_s
    return SiteSnapshot(
        site_id=f"private-node:{socket.gethostname()}",
        hostname=socket.gethostname(),
        cpuset=cpuset,
        memory_mb=memory_limit_mb,
        gpu_ids=gpu_ids,
        allocation_expires_at=expiry,
    )


def preflight_request(request: ResourceRequest,
                      site: SiteSnapshot) -> None:
    if request.mpi_ranks:
        raise ValueError("MPI_UNCERTIFIED: Stage 1 does not execute MPI")
    if request.cpu_cores > len(site.cpuset):
        raise ValueError(
            f"CPU request {request.cpu_cores} exceeds visible cpuset "
            f"capacity {len(site.cpuset)}")
    if request.memory_mb > site.memory_mb:
        raise ValueError(
            f"memory request {request.memory_mb} MiB exceeds site envelope "
            f"{site.memory_mb} MiB")
    if request.gpus > len(site.gpu_ids):
        raise ValueError(
            f"GPU request {request.gpus} exceeds visible GPUs "
            f"{len(site.gpu_ids)}")
    if site.allocation_expires_at is not None:
        remaining = site.allocation_expires_at - time.time()
        if request.walltime_s > remaining:
            raise ValueError(
                f"walltime request {request.walltime_s}s exceeds allocation "
                f"lifetime {remaining:.1f}s")


def _cgroup_v1_memory_limit_bytes() -> int | None:
    """Return an effective cgroup-v1 memory limit when one is configured."""
    cgroup_path: str | None = None
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            _hierarchy, controllers, path = line.split(":", 2)
            if "memory" in controllers.split(","):
                cgroup_path = path.lstrip("/")
                break
    except (OSError, ValueError):
        return None
    if cgroup_path is None:
        return None
    limit_path = (Path("/sys/fs/cgroup/memory") / cgroup_path
                  / "memory.limit_in_bytes")
    try:
        limit = int(limit_path.read_text().strip())
        physical = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None
    # Kernels commonly encode "unlimited" as a huge value near LONG_MAX.
    return limit if 0 < limit < physical else None


def _device_gpu_ids() -> tuple[str, ...]:
    return tuple(
        path.name.removeprefix("nvidia")
        for path in sorted(Path("/dev").glob("nvidia[0-9]*")))
