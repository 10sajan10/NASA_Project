"""One-shot installer for the WRF-SFIRE stack used by `WRFSFireAdapter`.

Translates the openwfm.org/wrfx wiki's "Running WRF-SFIRE with real data"
checklist (https://wiki.openwfm.org) into Python so the adapter can
provision its own dependencies.

What it installs (idempotent — re-running with everything already present
is cheap):

  1. WRF-SFIRE        cloned from github.com/openwfm/WRF-SFIRE,
                       compiled `em_fire` + `em_real` -> main/{wrf,
                       ideal, real}.exe
  2. WPS              cloned from github.com/openwfm/WPS, compiled ->
                       geogrid.exe, ungrib.exe, metgrid.exe
  3. WPS_GEOG         downloaded from demo.openwfm.org/web/wrfx and
                       extracted (~50 GB unpacked; the bootstrap skips
                       the download if the directory already exists)

Each step is heavy:
  * The clones add ~1.5 GB of source.
  * Compile takes ~30 minutes (em_real + em_fire + WPS).
  * WPS_GEOG is multi-tens-of-GB.

So the bootstrap function:
  * is **explicit** — callers invoke it once on a new machine; nothing
    in the engine auto-runs it.
  * is **idempotent** — uses skip-if-present checks so re-runs only do
    missing work.
  * supports `dry_run=True` so unit tests can verify the install plan
    without actually shelling out to git/wget/make.

Limitations (intentional, to keep this module short and predictable):
  * Configure is interactive in the upstream Makefiles. We feed answers
    via stdin; if your environment needs different compiler choices,
    pass them via `wrf_configure_input` / `wps_configure_input`.
  * The bootstrap does not provision Intel/GNU compilers, NetCDF,
    Jasper, or MPI — those are system packages outside our control.
    The wiki lists what to install via your OS package manager.
  * WRF-SFIRE is configured in classic NetCDF mode by default. That keeps
    this repo on the portable path we verified on CHPC and avoids WRF's
    fragile explicit HDF5 link flags (`-lhdf5*`), which do not match the
    Debian/Ubuntu `hdf5_serial` library names.
  * GRIB acquisition (ungrib input) is NOT covered. Met-data ingest is
    a separate concern; this module only builds the binaries.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------- urls
WRF_SFIRE_REPO = "https://github.com/openwfm/WRF-SFIRE"
WPS_REPO = "https://github.com/openwfm/WPS"
WPS_GEOG_URL = "https://demo.openwfm.org/web/wrfx/WPS_GEOG.tbz"


# Defaults match the openwfm wiki's recommendations:
#   * WRF-SFIRE configure: 32 (GNU gfortran/gcc serial) + 0 (no nesting).
#     This is the portable local/dev default verified for the adapter.
#     Override via `wrf_configure_input` for Intel or MPI builds.
#   * WPS configure: 1 (GNU gfortran/gcc serial). Wiki recommends 17
#     (Intel serial); override for that.
_DEFAULT_WRF_CONFIGURE_INPUT = "32\n0\n"
_DEFAULT_WPS_CONFIGURE_INPUT = "1\n"


# --------------------------------------------------------------------- plan
@dataclass
class BootstrapPlan:
    """What the bootstrap intends to do, in order.

    Tests use this to verify the install plan without actually executing
    git/wget/make. `bootstrap_wrf_sfire_stack(dry_run=True)` returns a
    plan; passing `dry_run=False` (the default) executes it.
    """
    install_root: Path
    wrf_sfire_dir: Path
    wps_dir: Path
    wps_geog_dir: Path
    actions: list[str] = field(default_factory=list)

    def add(self, action: str) -> None:
        self.actions.append(action)

    def __str__(self) -> str:
        return "\n".join(self.actions) if self.actions else "(no actions)"


# --------------------------------------------------------------------- main
def bootstrap_wrf_sfire_stack(
    install_root: str | Path = "wrf-sfire-stack",
    *,
    skip_wrf: bool = False,
    skip_wps: bool = False,
    skip_wps_geog: bool = False,
    wrf_configure_input: str = _DEFAULT_WRF_CONFIGURE_INPUT,
    wps_configure_input: str = _DEFAULT_WPS_CONFIGURE_INPUT,
    netcdf_env: Optional[dict[str, str]] = None,
    dry_run: bool = False,
) -> BootstrapPlan:
    """Provision WRF-SFIRE + WPS + WPS_GEOG under `install_root`.

    Returns a `BootstrapPlan` recording what was (or would be) done.

    Parameters
    ----------
    install_root :
        Parent directory; subdirs `WRF-SFIRE/`, `WPS/`, `WPS_GEOG/`
        get created inside it.
    skip_wrf / skip_wps / skip_wps_geog :
        Per-component opt-outs. Useful when one is already installed
        elsewhere on the system.
    wrf_configure_input / wps_configure_input :
        Newline-separated answers fed to the interactive
        `./configure` scripts. Defaults pick GNU compilers.
    netcdf_env :
        Optional environment variables for the build (NETCDF, JASPERLIB,
        JASPERINC, etc.). Merged into `os.environ` before each compile
        invocation. HDF5/HD5 are intentionally ignored on the default
        path because WRF-SFIRE is built with `NETCDF_classic=1`.
    dry_run :
        If True, return the plan without executing the heavy clone /
        configure / compile / download steps. The plan still creates
        the install_root directory (cheap) so paths can be inspected.
    """
    root = Path(install_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    plan = BootstrapPlan(
        install_root=root,
        wrf_sfire_dir=root / "WRF-SFIRE",
        wps_dir=root / "WPS",
        wps_geog_dir=root / "WPS_GEOG",
    )

    env = dict(os.environ)
    if netcdf_env:
        env.update(netcdf_env)
    _force_classic_netcdf(env, plan)

    if not skip_wrf:
        _bootstrap_wrf_sfire(plan, env, wrf_configure_input, dry_run)
    else:
        plan.add(f"skip WRF-SFIRE (already present or skipped)")

    if not skip_wps:
        _bootstrap_wps(plan, env, wps_configure_input, dry_run)
    else:
        plan.add("skip WPS (already present or skipped)")

    if not skip_wps_geog:
        _bootstrap_wps_geog(plan, dry_run)
    else:
        plan.add("skip WPS_GEOG (already present or skipped)")

    return plan


# --------------------------------------------------------------------- env
def _force_classic_netcdf(env: dict[str, str], plan: BootstrapPlan) -> None:
    """Keep the default build on the proven NetCDF-classic path.

    WRF can build with NetCDF4/HDF5, but the upstream configure script
    emits generic `-lhdf5*` flags. On this CHPC/Ubuntu-style environment
    the available libraries are named `libhdf5_serial*`, so explicit HDF5
    linking fails even though NetCDF itself can use HDF5 internally.
    """
    env["NETCDF_classic"] = "1"
    removed = [name for name in ("HDF5", "HD5") if env.pop(name, None)]
    if removed:
        plan.add("ignore HDF5/HD5 for WRF-SFIRE bootstrap "
                  "(using NETCDF_classic=1)")
    else:
        plan.add("set NETCDF_classic=1 for WRF-SFIRE bootstrap")


# --------------------------------------------------------------------- WRF
def _bootstrap_wrf_sfire(plan: BootstrapPlan, env: dict[str, str],
                          configure_input: str, dry_run: bool) -> None:
    target = plan.wrf_sfire_dir
    if not target.exists():
        plan.add(f"clone WRF-SFIRE -> {target}")
        if not dry_run:
            _run(["git", "clone", WRF_SFIRE_REPO, str(target)])
    else:
        plan.add(f"WRF-SFIRE present at {target} (skip clone)")

    main_dir = target / "main"
    needed = ["wrf.exe", "ideal.exe", "real.exe"]
    have_all = main_dir.exists() and all((main_dir / b).exists()
                                          for b in needed)
    if have_all:
        plan.add(f"WRF-SFIRE binaries already built (skip compile)")
        return

    # WRF's `./configure` requires NETCDF to be a directory with include/
    # + lib/ holding the right headers / .mod / .so files. On
    # Ubuntu/Debian the system layout is split across /usr/include and
    # /usr/lib/<triplet>, so build a symlink stub when needed.
    _ensure_lib_stub(
        env, plan, env_var="NETCDF",
        header_check="netcdf.inc",
        candidate_includes=[Path("/usr/include")],
        candidate_libs=[Path("/usr/lib/x86_64-linux-gnu"),
                         Path("/usr/lib64"),
                         Path("/usr/lib")],
        dry_run=dry_run)

    # If a previous compile failed mid-link, configure.wrf carries the old
    # paths and the .o files are stale. Re-run configure (overwrites
    # configure.wrf) and clean before recompiling. Detect this state by
    # configure.wrf existing but the target binaries missing.
    if (target / "configure.wrf").exists() and not have_all:
        plan.add("./clean -a (stale partial build detected)")
        if not dry_run:
            _run(["./clean", "-a"], cwd=target, env=env)

    # Need to configure + compile.
    plan.add(f"configure WRF-SFIRE (stdin={configure_input!r})")
    plan.add("compile em_fire (~15 min)")
    plan.add("compile em_real (~15 min)")
    if dry_run:
        return

    # ./configure is interactive; feed answers via stdin.
    _run(["./configure"], cwd=target, env=env, input_text=configure_input)
    _run(["./compile", "em_fire"], cwd=target, env=env, capture_log=True,
         log_name="compile_em_fire.log")
    _run(["./compile", "em_real"], cwd=target, env=env, capture_log=True,
         log_name="compile_em_real.log")


def _ensure_lib_stub(env: dict[str, str], plan: BootstrapPlan,
                      *,
                      env_var: str,
                      header_check: str,
                      candidate_includes: list[Path],
                      candidate_libs: list[Path],
                      lib_pattern: str = "lib*.so*",
                      dry_run: bool) -> None:
    """If `env[env_var]` points at a missing / improperly-laid-out
    directory, auto-build a symlink stub:

        $<env_var>/include -> first candidate_include that contains
                               `header_check`
        $<env_var>/lib     -> first candidate_lib that contains a file
                               matching `lib_pattern`

    This sidesteps the Ubuntu/Debian split layout (for example, NetCDF in
    /usr/include + /usr/lib/<triplet>). Skipped when the env var isn't
    set, the target already looks usable, or dry_run=True is in effect.
    """
    val = env.get(env_var)
    if not val:
        return
    root = Path(val).expanduser()
    if (root / "include" / header_check).exists():
        return  # already usable

    sys_inc = next((c for c in candidate_includes
                     if (c / header_check).exists()), None)
    if sys_inc is None:
        plan.add(f"WARNING: {header_check} not found in {candidate_includes} "
                  f"for {env_var}; install the dev package or set "
                  f"{env_var} to a usable root")
        return

    sys_lib = next(
        (c for c in candidate_libs
         if c.exists() and any(c.glob(lib_pattern))),
        None)
    if sys_lib is None:
        plan.add(f"WARNING: no {lib_pattern} found in {candidate_libs} "
                  f"for {env_var}")
        return

    plan.add(f"prepare {env_var} stub at {root} "
              f"(include -> {sys_inc}, lib -> {sys_lib})")
    if dry_run:
        return

    root.mkdir(parents=True, exist_ok=True)
    for name, src in (("include", sys_inc), ("lib", sys_lib)):
        link = root / name
        if link.is_symlink() or link.exists():
            try:
                link.unlink()
            except (IsADirectoryError, OSError):
                continue
        link.symlink_to(src)


# --------------------------------------------------------------------- WPS
def _bootstrap_wps(plan: BootstrapPlan, env: dict[str, str],
                    configure_input: str, dry_run: bool) -> None:
    target = plan.wps_dir
    if not target.exists():
        plan.add(f"clone WPS -> {target}")
        if not dry_run:
            _run(["git", "clone", WPS_REPO, str(target)])
    else:
        plan.add(f"WPS present at {target} (skip clone)")

    needed = ["geogrid.exe", "ungrib.exe", "metgrid.exe"]
    have_all = all((target / b).exists() for b in needed)
    if have_all:
        plan.add("WPS binaries already built (skip compile)")
        return

    plan.add(f"configure WPS (WRF_DIR={plan.wrf_sfire_dir}, "
              f"stdin={configure_input!r})")
    plan.add("compile WPS (~5 min)")
    if dry_run:
        return

    wps_env = dict(env)
    wps_env["WRF_DIR"] = str(plan.wrf_sfire_dir)
    _run(["./configure"], cwd=target, env=wps_env,
         input_text=configure_input)
    _run(["./compile"], cwd=target, env=wps_env, capture_log=True,
         log_name="compile_wps.log")


# --------------------------------------------------------------------- GEOG
def _bootstrap_wps_geog(plan: BootstrapPlan, dry_run: bool) -> None:
    target = plan.wps_geog_dir
    if target.exists() and any(target.iterdir()):
        plan.add(f"WPS_GEOG present at {target} (skip download)")
        return

    plan.add(f"download WPS_GEOG.tbz from {WPS_GEOG_URL}")
    plan.add(f"extract WPS_GEOG into {plan.install_root}")
    if dry_run:
        return

    tbz_path = plan.install_root / "WPS_GEOG.tbz"
    if not tbz_path.exists():
        urllib.request.urlretrieve(WPS_GEOG_URL, tbz_path)  # noqa: S310
    with tarfile.open(tbz_path, "r:bz2") as tar:
        tar.extractall(plan.install_root)


# --------------------------------------------------------------------- subp
def _run(cmd: list[str],
          *,
          cwd: Optional[Path] = None,
          env: Optional[dict[str, str]] = None,
          input_text: Optional[str] = None,
          capture_log: bool = False,
          log_name: Optional[str] = None) -> None:
    """Thin wrapper around subprocess.run with sensible defaults.

    When `capture_log` is True, stdout/stderr are streamed both to a
    log file in `cwd` (`log_name`, default = first arg + ".log") and to
    the parent process — matches the wiki's `./compile foo >& foo.log`
    pattern."""
    if capture_log and cwd is not None:
        log_path = cwd / (log_name or f"{Path(cmd[0]).name}.log")
        with open(log_path, "w") as fh:
            subprocess.run(
                cmd, cwd=cwd, env=env, input=input_text, text=True,
                stdout=fh, stderr=subprocess.STDOUT, check=True)
    else:
        subprocess.run(
            cmd, cwd=cwd, env=env, input=input_text, text=True,
            check=True)
