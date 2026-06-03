"""HPC system auto-detection and environment setup.

Detects the current HPC system from hostname / Lmod env variables and
returns the correct module list, MPI launcher, SLURM defaults, and
WRF configure answers for that system.

Supported systems (add new entries to _PROFILES as needed):
  chpc_utah    - University of Utah CHPC (notchpeak, kingspeak, etc.)
  stampede3    - TACC Stampede3
  derecho      - NCAR Derecho
  frontier     - ORNL Frontier
  generic_slurm - any SLURM cluster — user must supply modules manually

Usage
-----
    from hpc.profiles import detect_profile, load_modules

    profile = detect_profile()
    print(profile.name, profile.mpi_launcher)
    load_modules(profile)          # runs 'module load ...' in subprocess
    env = profile.compile_env()    # dict to pass to subprocess.run(env=...)
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class HPCProfile:
    """All system-specific knobs for one HPC environment."""

    name: str

    # Modules to load before compiling or running WRF.
    # Order matters — MPI must come after compiler.
    modules: list[str] = field(default_factory=list)

    # MPI launcher (e.g. "mpirun", "srun", "ibrun").
    mpi_launcher: str = "mpirun"

    # Number of MPI ranks to use when none is specified by the caller.
    default_np: int = 8

    # SLURM defaults for this system.
    slurm_partition: str = "notchpeak"
    slurm_account: str = ""
    slurm_walltime: str = "04:00:00"
    slurm_nodes: int = 1
    slurm_ntasks_per_node: int = 56
    slurm_mem: str = "120GB"
    slurm_constraint: str = ""

    # Answer string piped to WRF's interactive ./configure.
    # Format:  "<option_number>\n<nesting_type>\n"
    #   nesting: 0=no nesting, 1=basic, 2=preset moves, 3=vortex following
    wrf_configure_input: str = "34\n1\n"   # GNU gfortran dmpar, basic nesting

    # Answer for WPS ./configure.
    wps_configure_input: str = "3\n"       # GNU gfortran with WRF_DIR

    # Extra env vars to inject during compile (on top of the shell env).
    extra_env: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    def compile_env(self) -> dict[str, str]:
        """Return os.environ merged with profile extras + HDF5 version check bypass."""
        env = dict(os.environ)
        env.update(self.extra_env)
        env.setdefault("HDF5_DISABLE_VERSION_CHECK", "1")
        env.setdefault("WRFIO_NCD_LARGE_FILE_SUPPORT", "1")
        return env

    def mpi_cmd(self, exe: str | Path, np: int | None = None) -> list[str]:
        """Build the MPI launch command for a given executable."""
        n = np if np is not None else self.default_np
        launcher = self.mpi_launcher
        if launcher in ("mpirun", "mpiexec"):
            return [launcher, "-np", str(n), str(exe)]
        if launcher == "srun":
            return ["srun", f"--ntasks={n}", str(exe)]
        if launcher == "ibrun":
            return ["ibrun", "-np", str(n), str(exe)]
        return [launcher, str(exe)]


# ------------------------------------------------------------------ registry

_PROFILES: dict[str, HPCProfile] = {

    "chpc_utah": HPCProfile(
        name="chpc_utah",
        modules=[
            "gcc/8.5.0",
            "openmpi/4.1.6",
            "hdf5/1.14.1-2",
            "netcdf-c/4.9.2",
            "netcdf-fortran/4.6.1",
        ],
        mpi_launcher="mpirun",
        default_np=56,
        slurm_partition="notchpeak",
        slurm_account="",            # user sets via --account or $SLURM_ACCOUNT
        slurm_walltime="08:00:00",
        slurm_nodes=1,
        slurm_ntasks_per_node=56,
        slurm_mem="120GB",
        wrf_configure_input="34\n1\n",
        wps_configure_input="3\n",
        extra_env={
            "HDF5_DISABLE_VERSION_CHECK": "1",
            "WRFIO_NCD_LARGE_FILE_SUPPORT": "1",
        },
    ),

    "stampede3": HPCProfile(
        name="stampede3",
        modules=[
            "intel/24.0",
            "impi/21.11",
            "netcdf/4.9.2",
            "hdf5/1.14.3",
            "jasper/2.0.33",
        ],
        mpi_launcher="ibrun",
        default_np=96,
        slurm_partition="skx",
        slurm_account="",
        slurm_walltime="08:00:00",
        slurm_nodes=2,
        slurm_ntasks_per_node=48,
        slurm_mem="192GB",
        wrf_configure_input="75\n1\n",   # Intel impi
        wps_configure_input="19\n",
        extra_env={"WRFIO_NCD_LARGE_FILE_SUPPORT": "1"},
    ),

    "derecho": HPCProfile(
        name="derecho",
        modules=[
            "gcc/12.2.0",
            "openmpi/4.1.6",
            "netcdf/4.9.2",
            "hdf5/1.12.2",
            "jasper/2.0.32",
        ],
        mpi_launcher="mpirun",
        default_np=128,
        slurm_partition="main",
        slurm_account="",
        slurm_walltime="06:00:00",
        slurm_nodes=2,
        slurm_ntasks_per_node=64,
        slurm_mem="256GB",
        wrf_configure_input="34\n1\n",
        wps_configure_input="3\n",
        extra_env={"WRFIO_NCD_LARGE_FILE_SUPPORT": "1"},
    ),

    "frontier": HPCProfile(
        name="frontier",
        modules=[
            "PrgEnv-gnu",
            "cray-mpich/8.1.27",
            "cray-hdf5-parallel/1.12.2.9",
            "cray-netcdf-hdf5parallel/4.9.0.9",
        ],
        mpi_launcher="srun",
        default_np=64,
        slurm_partition="batch",
        slurm_account="",
        slurm_walltime="04:00:00",
        slurm_nodes=1,
        slurm_ntasks_per_node=64,
        slurm_mem="512GB",
        wrf_configure_input="3\n1\n",    # Cray GNU
        wps_configure_input="3\n",
        extra_env={"WRFIO_NCD_LARGE_FILE_SUPPORT": "1"},
    ),

    "generic_slurm": HPCProfile(
        name="generic_slurm",
        modules=[],                      # user must provide via --modules
        mpi_launcher="mpirun",
        default_np=16,
        slurm_partition="compute",
        slurm_walltime="08:00:00",
        slurm_nodes=1,
        slurm_ntasks_per_node=16,
        slurm_mem="64GB",
        wrf_configure_input="34\n1\n",
        wps_configure_input="3\n",
    ),
}


# ------------------------------------------------------------------ detection

def detect_profile(override: str | None = None) -> HPCProfile:
    """Return the best-matching HPCProfile for the current machine.

    Detection order:
      1. `override` argument (profile name string)
      2. ``$HPC_PROFILE`` environment variable
      3. ``$LMOD_SYSTEM_NAME`` (set by Lmod on most HPC clusters)
      4. hostname pattern matching

    Falls back to ``generic_slurm`` if nothing matches.
    """
    name = (override
            or os.environ.get("HPC_PROFILE")
            or _detect_from_lmod()
            or _detect_from_hostname())
    if name and name in _PROFILES:
        return _PROFILES[name]
    return _PROFILES["generic_slurm"]


def _detect_from_lmod() -> str | None:
    sys_name = os.environ.get("LMOD_SYSTEM_NAME", "").lower()
    mapping = {
        "notchpeak": "chpc_utah",
        "kingspeak": "chpc_utah",
        "lonepeak":  "chpc_utah",
        "chpc":      "chpc_utah",
        "stampede3": "stampede3",
        "derecho":   "derecho",
        "frontier":  "frontier",
    }
    for fragment, profile_name in mapping.items():
        if fragment in sys_name:
            return profile_name
    return None


def _detect_from_hostname() -> str | None:
    try:
        hostname = socket.gethostname().lower()
    except Exception:
        return None
    patterns = {
        r"notchpeak|kingspeak|lonepeak|uofurc":  "chpc_utah",
        r"stampede3":                             "stampede3",
        r"derecho|casper":                        "derecho",
        r"frontier":                              "frontier",
    }
    for pat, name in patterns.items():
        if re.search(pat, hostname):
            return name
    return None


# ------------------------------------------------------------------ module loading

def load_modules(profile: HPCProfile, *, dry_run: bool = False) -> list[str]:
    """Load the profile's modules in the current shell via 'module load'.

    Because ``module`` is a shell function (not an executable), this
    uses the ``modulecmd python`` interface when available, then falls
    back to ``source`` + ``module load`` via a subshell.

    Returns the list of modules that were loaded (or would be loaded in
    dry_run mode).
    """
    if not profile.modules:
        return []

    if dry_run:
        return list(profile.modules)

    # Preferred: use 'module load' through a bash subshell so the loaded
    # environment variables propagate back through os.environ updates.
    _load_via_bash(profile.modules)
    return list(profile.modules)


def _load_via_bash(modules: list[str]) -> None:
    """Run 'module load <m>' for each module, capturing env changes."""
    lmod_sh = _find_lmod_init()
    if lmod_sh is None:
        # Lmod not available — silently skip. Compile env will still work
        # if the user has manually loaded the modules.
        return

    module_cmds = " && ".join(f"module load {m}" for m in modules)
    script = f"source {lmod_sh} && {module_cmds} && env"

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True)
    if result.returncode != 0:
        # Non-fatal: the modules might already be loaded.
        return

    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            os.environ[key] = val


def _find_lmod_init() -> Optional[str]:
    """Locate the Lmod bash initialisation script."""
    candidates = [
        os.environ.get("LMOD_PKG", ""),
        "/usr/share/lmod/lmod",
        "/opt/apps/lmod/lmod",
        "/software/lmod/lmod",
    ]
    for base in candidates:
        if not base:
            continue
        path = Path(base) / "init" / "bash"
        if path.exists():
            return str(path)
    # Try which modulecmd as a last resort
    mc = shutil.which("modulecmd")
    if mc:
        return None   # modulecmd exists but no init script — handled elsewhere
    return None


# ------------------------------------------------------------------ registry helpers

def available_profiles() -> list[str]:
    """Return names of all registered profiles."""
    return list(_PROFILES.keys())


def register_profile(profile: HPCProfile) -> None:
    """Add or replace a profile at runtime (useful for site-local config)."""
    _PROFILES[profile.name] = profile
