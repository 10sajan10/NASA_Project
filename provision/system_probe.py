"""System probe — detect what a host can offer for building/running WRF-SFIRE.

Pure stdlib so it runs on a bare login node before any venv or module is
loaded. It answers three questions:

  1. Can we run a CONTAINER here?  (apptainer / singularity / docker / podman)
  2. Can we BUILD FROM SOURCE here? (compilers, MPI, make, git, csh, wget…)
  3. What dependency libraries already exist? (netcdf, hdf5, jasper …)

The provisioning agent turns this report into a strategy. The probe makes
NO decisions and changes NO state — it only observes.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Optional


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def _version(cmd: list[str], pattern: str = r"(\d+\.\d+(\.\d+)?)") -> str:
    out = _run(cmd)
    m = re.search(pattern, out)
    return m.group(1) if m else ""


@dataclass
class Tool:
    name: str
    path: Optional[str] = None
    version: str = ""

    @property
    def present(self) -> bool:
        return self.path is not None

    @classmethod
    def detect(cls, name: str, version_cmd: Optional[list[str]] = None) -> "Tool":
        p = _which(name)
        v = _version(version_cmd) if (p and version_cmd) else ""
        return cls(name=name, path=p, version=v)


@dataclass
class SystemProbe:
    """A snapshot of the host's build/run capabilities."""

    # --- platform ---
    os_name: str = ""
    os_version: str = ""
    kernel: str = ""
    arch: str = ""
    cpu_count: int = 0
    mem_gb: float = 0.0

    # --- container runtimes ---
    containers: dict[str, Tool] = field(default_factory=dict)

    # --- toolchain ---
    compilers: dict[str, Tool] = field(default_factory=dict)
    mpi: dict[str, Tool] = field(default_factory=dict)
    build_tools: dict[str, Tool] = field(default_factory=dict)

    # --- existing dependency libs (we do NOT rely on these, but we report) ---
    libs: dict[str, Tool] = field(default_factory=dict)

    # --- module system ---
    has_lmod: bool = False

    # ------------------------------------------------------------------
    @classmethod
    def probe(cls) -> "SystemProbe":
        p = cls()

        # platform
        p.os_name, p.os_version = _os_release()
        p.kernel = platform.release()
        p.arch = platform.machine()
        p.cpu_count = os.cpu_count() or 1
        p.mem_gb = _mem_gb()

        # container runtimes (preferred path for system-agnostic deploy)
        for rt in ("apptainer", "singularity", "docker", "podman"):
            p.containers[rt] = Tool.detect(rt, [rt, "--version"])

        # compilers
        p.compilers["gcc"] = Tool.detect("gcc", ["gcc", "-dumpfullversion"])
        p.compilers["gfortran"] = Tool.detect("gfortran", ["gfortran", "-dumpfullversion"])
        p.compilers["g++"] = Tool.detect("g++", ["g++", "-dumpfullversion"])

        # MPI
        p.mpi["mpif90"] = Tool.detect("mpif90", ["mpif90", "--version"])
        p.mpi["mpicc"] = Tool.detect("mpicc", ["mpicc", "--version"])
        p.mpi["mpirun"] = Tool.detect("mpirun", ["mpirun", "--version"])

        # build tools — note: WRF's ./compile REQUIRES csh
        for t in ("make", "git", "csh", "m4", "cmake", "wget", "curl",
                  "tar", "perl", "cpp"):
            p.build_tools[t] = Tool.detect(t)

        # existing dependency libraries (reported, not depended on)
        p.libs["netcdf-c"] = _lib_from_config("nc-config")
        p.libs["netcdf-fortran"] = _lib_from_config("nf-config")
        p.libs["hdf5"] = Tool.detect("h5cc")
        p.libs["jasper"] = _pkgconfig_lib("jasper")
        p.libs["zlib"] = _pkgconfig_lib("zlib")
        p.libs["libpng"] = _pkgconfig_lib("libpng")

        p.has_lmod = bool(os.environ.get("LMOD_CMD") or _which("lmod"))
        return p

    # ------------------------------------------------------------------
    # capability questions
    # ------------------------------------------------------------------
    def best_container_runtime(self) -> Optional[str]:
        """HPC-preferred order: apptainer > singularity > podman > docker."""
        for rt in ("apptainer", "singularity", "podman", "docker"):
            if self.containers.get(rt, Tool(rt)).present:
                return rt
        return None

    def can_containerize(self) -> bool:
        return self.best_container_runtime() is not None

    def can_build_from_source(self) -> tuple[bool, list[str]]:
        """True if the minimum toolchain to build the full stack is present.
        Returns (ok, missing_tool_names)."""
        required = {
            "gcc": self.compilers["gcc"],
            "gfortran": self.compilers["gfortran"],
            "g++": self.compilers["g++"],
            "mpif90": self.mpi["mpif90"],
            "make": self.build_tools["make"],
            "git": self.build_tools["git"],
            "csh": self.build_tools["csh"],   # WRF ./compile needs csh
            "m4": self.build_tools["m4"],
        }
        missing = [n for n, t in required.items() if not t.present]
        return (len(missing) == 0, missing)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    # ------------------------------------------------------------------
    def render(self) -> str:
        L = []
        L.append("=" * 66)
        L.append("SYSTEM PROBE")
        L.append("=" * 66)
        L.append(f"OS        : {self.os_name} {self.os_version} "
                 f"({self.arch}, kernel {self.kernel})")
        L.append(f"Resources : {self.cpu_count} cores, {self.mem_gb:.0f} GB RAM")
        L.append(f"Lmod      : {'yes' if self.has_lmod else 'no'}")

        L.append("\nContainer runtimes (preferred for portability):")
        for rt, t in self.containers.items():
            mark = "✓" if t.present else "·"
            L.append(f"  {mark} {rt:12s} {t.version or ''}")
        best = self.best_container_runtime()
        L.append(f"  -> best: {best or 'NONE'}")

        L.append("\nToolchain (for source build):")
        for grp in (self.compilers, self.mpi):
            for n, t in grp.items():
                mark = "✓" if t.present else "✗"
                L.append(f"  {mark} {n:12s} {t.version or ''}")
        L.append("  build tools: " + ", ".join(
            f"{n}{'✓' if t.present else '✗'}"
            for n, t in self.build_tools.items()))

        L.append("\nExisting dependency libs (reported; agent does NOT rely on them):")
        for n, t in self.libs.items():
            mark = "✓" if t.present else "·"
            L.append(f"  {mark} {n:14s} {t.path or ''}")

        ok, missing = self.can_build_from_source()
        L.append("\nVerdict:")
        L.append(f"  containerize     : {'YES' if self.can_containerize() else 'no'}"
                 f"  ({best or 'no runtime'})")
        L.append(f"  build-from-source: {'YES' if ok else 'NO'}"
                 + (f"  (missing: {', '.join(missing)})" if missing else ""))
        return "\n".join(L)


# ---------------------------------------------------------------- helpers
def _os_release() -> tuple[str, str]:
    try:
        data = {}
        with open("/etc/os-release") as fh:
            for line in fh:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    data[k] = v.strip('"')
        return data.get("NAME", platform.system()), data.get("VERSION_ID", "")
    except Exception:
        return platform.system(), platform.release()


def _mem_gb() -> float:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / 1024 / 1024
    except Exception:
        pass
    return 0.0


def _lib_from_config(config_tool: str) -> Tool:
    """netcdf-c/-fortran expose nc-config/nf-config with --version + --prefix."""
    path = _which(config_tool)
    if not path:
        return Tool(config_tool)
    ver = _version([config_tool, "--version"])
    prefix = _run([config_tool, "--prefix"])
    return Tool(name=config_tool, path=prefix or path, version=ver)


def _pkgconfig_lib(name: str) -> Tool:
    pc = _which("pkg-config")
    if pc and subprocess.run([pc, "--exists", name],
                             capture_output=True).returncode == 0:
        ver = _run([pc, "--modversion", name])
        return Tool(name=name, path="(pkg-config)", version=ver)
    # jasper/zlib often present without .pc; check common headers
    for inc in ("/usr/include", "/usr/local/include"):
        if name == "jasper" and os.path.exists(f"{inc}/jasper/jasper.h"):
            return Tool(name=name, path=inc)
        if name == "zlib" and os.path.exists(f"{inc}/zlib.h"):
            return Tool(name=name, path=inc)
        if name == "libpng" and os.path.exists(f"{inc}/png.h"):
            return Tool(name=name, path=inc)
    return Tool(name=name)


if __name__ == "__main__":
    print(SystemProbe.probe().render())
