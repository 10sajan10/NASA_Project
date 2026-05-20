"""WRF-SFIRE adapter.

Stages cube inputs into WRF-SFIRE NetCDF/namelist files, runs
``real.exe`` (or ``ideal.exe`` for an idealised fallback) followed by
``wrf.exe``, and parses ``wrfout`` back into cube variables.

Consumes:
  ``ignition_t0``, ``nfuel_cat``, ``dem``, optional
  ``wind_speed_ms`` / ``wind_dir_deg`` (used only for the ideal-fallback
  ``input_sounding`` patch; real.exe gets atmospheric state from
  ``met_em.d01.*`` files instead).

Produces:
  ``arrival_s`` from ``TIGN_G``, ``fire_area`` from ``FIRE_AREA``.

Setup notes (cloning WRF-SFIRE / WPS / WPS_GEOG, compile flags,
``module load`` recipes, WRFx context) live in ``docs/wrf_sfire.md``;
keeping them there lets this file stay focused on runtime translation.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np

from engine.contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    Request,
    VarSpec,
)
from engine.data_adapter import DataAdapter, DataNeed
from engine.log import get_logger
from engine.model_adapter import ModelAdapter


_log = get_logger(__name__)


# Large sentinel placed in TIGN_IN for cells that did NOT ignite from the
# asteroid pulse. Must be > fire_tign_in_time so SFIRE leaves them
# un-ignited and lets its spread physics reach them naturally.
_UNIGNITED_SENTINEL_S = 1.0e9


class WRFSFireAdapter(ModelAdapter):
    """WRF-SFIRE driven by an asteroid-derived spatial ignition field."""

    name = "wrf_sfire_asteroid"

    data_adapter = DataAdapter([
        DataNeed("ignition_t0", kind="static",
                  units="s",
                  description="Per-cell ignition time (s); NaN = un-ignited"),
        DataNeed("nfuel_cat", kind="static",
                  description="Fuel category raster (1..N burnable, "
                              "no-fuel sentinel for non-burnable cells)"),
        DataNeed("dem", kind="static", units="m",
                  description="Surface elevation"),
        DataNeed("wind_speed_ms", kind="time", required=False,
                  description="Optional canonical wind speed series; "
                              "if absent the namelist defaults are used"),
        DataNeed("wind_dir_deg", kind="time", required=False),
    ])

    produces = (
        VarSpec("arrival_s", kind="static", dtype="float32", units="s",
                merge_policy=MergePolicy.MONOTONE_MIN,
                description="Fire arrival time from WRF-SFIRE TIGN_G"),
        VarSpec("fire_area", kind="static", dtype="float32", units="0..1",
                merge_policy=MergePolicy.MONOTONE_MAX,
                description="Cell-wise burned fraction from FIRE_AREA"),
    )

    capabilities = ProducerCapabilities(
        tile_parallel=False,        # coupled spatial integrator
        cost_hint=CostHint.CPU,
        memory_budget_mb=4096,
        iterative=False)

    # ------------------------------------------------------------------
    def __init__(self,
                 *,
                 sfire_dir: str | Path,
                 ideal_cmd: Optional[list[str]] = None,
                 real_cmd: Optional[list[str]] = None,
                 wrf_cmd: list[str] | None = None,
                 sim_seconds: int = 3 * 3600,
                 history_interval_s: int = 120,
                 fire_mesh_ratio: int = 4,
                 namelist_template: str = "namelist.input_ignite_from_tign_in",
                 fire_namelist: str = "namelist.fire",
                 sounding: str = "input_sounding",
                 met_em_dir: str | Path | None = None,
                 namelist_overrides: Optional[dict[str, str]] = None,
                 fire_dem_driver=None,
                 stage_root: str | Path | None = None,
                 keep_stage: bool = False) -> None:
        """
        sfire_dir : directory holding the WRF-SFIRE template files
                     (namelist.input, namelist.fire, input_sounding,
                      and the wrf.exe/ideal.exe binaries when present)
        ideal_cmd : command to run ideal.exe. None skips ideal (test mode);
                     the producer then builds wrfinput from scratch unless
                     real_cmd is selected.
        real_cmd  : command to run real.exe. When `met_em_dir` is also
                     given, the adapter takes the real-data path
                     (real.exe consumes met_em; cube wind is no longer
                     a gate on this).
        wrf_cmd   : command to run wrf.exe (e.g. ["./wrf.exe"]).
        sim_seconds : total fire-spread simulation length in seconds.
        history_interval_s : how often SFIRE writes a wrfout history frame.
        fire_mesh_ratio : SFIRE's fire mesh refinement factor over the
                          atmosphere mesh (typically 4).
        namelist_template : template namelist.input in sfire_dir. The
                            shipped 'namelist.input_ignite_from_tign_in'
                            is the canonical TIGN_IN-driven scenario.
        met_em_dir : directory of prebuilt WPS ``met_em.d01.*`` files.
                     Required for the real.exe path; they are
                     symlinked / copied into the stage before real.exe
                     runs.
        namelist_overrides : extra ``key=value`` lines to inject into
                              namelist.input after the standard patches.
        fire_dem_driver : optional ``DEMDriver``-shaped object exposing
                          ``fetch_to_array(xmin, ymin, xmax, ymax,
                          width, height, sr)``. When provided, ZSF is
                          sampled at the **fire mesh** ``(H*fmr, W*fmr)``
                          rather than nearest-neighbour upsampled from
                          the atmosphere DEM. Cube ``dem`` (at the
                          coarser atmosphere mesh) is still consumed
                          for HGT-side calculations.
        """
        self.sfire_dir = Path(sfire_dir)
        self.ideal_cmd = ideal_cmd
        self.real_cmd = real_cmd
        self.wrf_cmd = wrf_cmd or ["./wrf.exe"]
        self.sim_seconds = int(sim_seconds)
        self.history_interval_s = int(history_interval_s)
        self.fmr = int(fire_mesh_ratio)
        self.namelist_template = namelist_template
        self.fire_namelist = fire_namelist
        self.sounding = sounding
        self.met_em_dir = Path(met_em_dir) if met_em_dir is not None else None
        self.namelist_overrides = dict(namelist_overrides or {})
        self.fire_dem_driver = fire_dem_driver
        self.stage_root = Path(stage_root) if stage_root is not None else None
        self.keep_stage = bool(keep_stage)

    # ============================================================ BOOTSTRAP
    @classmethod
    def bootstrap(cls,
                   install_root: str | Path = "wrf-sfire-stack",
                   *,
                   dry_run: bool = False,
                   **kwargs):
        """Provision WRF-SFIRE + WPS + WPS_GEOG under `install_root`.

        Thin pass-through to
        ``models.wrf_sfire_bootstrap.bootstrap_wrf_sfire_stack``. See
        ``docs/wrf_sfire.md`` for what the bootstrap does.
        """
        from .wrf_sfire_bootstrap import bootstrap_wrf_sfire_stack
        return bootstrap_wrf_sfire_stack(
            install_root, dry_run=dry_run, **kwargs)

    # ============================================================ STAGE
    def stage_inputs(self, grid, inputs: dict[str, Any],
                     request: Request, stage: Path) -> None:
        """Assemble a complete WRF-SFIRE stage dir.

        Order is significant:

          1. Copy template files (namelist.input, namelist.fire,
             input_sounding) into ``stage``.
          2. Patch ``input_sounding`` with cube wind at scenario start
             (used by the ideal-fallback path only).
          3. Patch ``namelist.input`` with grid dims, calendar times,
             time step, ``fire_tign_in_time``, ``fire_fuel_read``, etc.
          4. Build ``wrfinput_d01.nc`` — via ``real.exe`` if
             ``met_em_dir`` is configured, else ``ideal.exe``.
          5. Inject TIGN_IN / NFUEL_CAT / ZSF into ``wrfinput_d01.nc``.
        """
        self._copy_templates(stage)

        used_wind = self._patch_input_sounding_with_wind(
            stage / "input_sounding", inputs, request)
        _log.info(
            "[wrf_sfire] %s",
            f"input_sounding patched with cube wind at {request.t_start}"
            if used_wind else "using template sounding")

        fire_tign_in_time = self._patch_namelist(
            stage / "namelist.input", grid, inputs, request)

        self._make_wrfinput(stage, grid)
        self._inject_sfire_inputs(
            stage / "wrfinput_d01.nc", grid, inputs, fire_tign_in_time)

    # ============================================================ RUN
    def run_model(self, stage: Path, request: Request) -> Path:
        subprocess.run(self._cmd_for_stage(self.wrf_cmd),
                       cwd=stage, check=True)
        return stage

    # ============================================================ PARSE
    def parse_outputs(self, output_path: Path,
                      grid) -> dict[str, Any]:
        wrfouts = sorted(output_path.glob("wrfout_d01_*"))
        if not wrfouts:
            raise FileNotFoundError(
                f"no wrfout_d01_* found in {output_path}")
        latest = wrfouts[-1]

        import netCDF4 as nc
        with nc.Dataset(latest) as ds:
            tign_fire = self._read_last_frame(ds, "TIGN_G")
            fire_area_fire = self._read_last_frame(ds, "FIRE_AREA")

        # Downsample fire-mesh -> atmosphere-mesh = cube-grid.
        H, W = grid.shape
        arrival_s = _block_reduce(
            tign_fire, (self.fmr, self.fmr), op="min")[:H, :W]
        fire_area = _block_reduce(
            fire_area_fire, (self.fmr, self.fmr), op="mean")[:H, :W]

        # Un-ignited cells: SFIRE writes the same large sentinel back to
        # TIGN_G. Surface as NaN so downstream consumers don't treat
        # them as arrival-time 1e9.
        arrival_s = np.where(arrival_s > 0.99 * _UNIGNITED_SENTINEL_S,
                              np.nan, arrival_s).astype("float32")
        return {
            "arrival_s": arrival_s,
            "fire_area": fire_area.astype("float32"),
        }

    # ============================================================ helpers
    def _copy_templates(self, stage: Path) -> None:
        """Copy the three text templates into the stage dir."""
        files = {
            self.namelist_template: "namelist.input",
            self.fire_namelist:     "namelist.fire",
            self.sounding:          "input_sounding",
        }
        for src_name, dst_name in files.items():
            src = self.sfire_dir / src_name
            if not src.exists():
                raise FileNotFoundError(
                    f"WRF-SFIRE template missing: {src}")
            shutil.copy(src, stage / dst_name)

    def _patch_namelist(self, path: Path, grid, inputs: dict[str, Any],
                         request: Request) -> float:
        """Write the cube-aware namelist.input and return
        ``fire_tign_in_time`` (the value SFIRE compares TIGN_IN against
        to decide which cells are pre-ignited)."""
        H, W = grid.shape

        ign = np.asarray(inputs["ignition_t0"], dtype="float32")
        finite = ign[np.isfinite(ign) & (ign >= 0)]
        max_ign = float(finite.max()) if finite.size else 0.0
        # 1-second margin so the latest asteroid ignition is still
        # treated as inside the pre-ignition window.
        fire_tign_in_time = max_ign + 1.0

        # CFL-stable integer time step from grid spacing. Rule of thumb:
        # ~6 s per km of dx. Floor at 1 s. Template ships 0.25 s (60-m
        # ideal case); without rescaling we'd over-resolve real grids.
        dx_km = float(grid.pixel_m) / 1000.0
        time_step_s = max(1, int(round(6.0 * dx_km)))

        patches: dict[str, str] = {
            "run_seconds": str(self.sim_seconds),
            "run_minutes": "0",
            "run_hours":   "0",
            "run_days":    "0",
            "end_second":  str(self.sim_seconds % 60),
            "end_minute":  str((self.sim_seconds // 60) % 60),
            "end_hour":    str(self.sim_seconds // 3600),
            "history_interval_s": str(self.history_interval_s),
            "e_we":        str(W + 1),
            "e_sn":        str(H + 1),
            "dx":          str(float(grid.pixel_m)),
            "dy":          str(float(grid.pixel_m)),
            "sr_x":        str(self.fmr),
            "sr_y":        str(self.fmr),
            # CFL-aware integer dt; drop the template's fractional override.
            "time_step":           str(time_step_s),
            "time_step_fract_num": "0",
            "time_step_fract_den": "1",
            "fire_tign_in_time":   f"{fire_tign_in_time:.3f}",
            "fire_num_ignitions":  "0",  # no point-source ignitions
            # CRITICAL: read NFUEL_CAT from wrfinput. Template default
            # (=0) uses uniform fire_fuel_cat and would silently nullify
            # the LANDFIRE upstream.
            "fire_fuel_read":      "-1",
        }

        # Calendar times from the request. real.exe requires these to
        # match the met_em.d01.* file timestamps; ideal.exe ignores
        # them but writing for both keeps the namelist self-consistent.
        if request is not None and request.t_start is not None:
            t0 = request.t_start
            t1 = t0 + timedelta(seconds=self.sim_seconds)
            patches.update({
                "start_year":   f"{t0.year:04d}",
                "start_month":  f"{t0.month:02d}",
                "start_day":    f"{t0.day:02d}",
                "start_hour":   f"{t0.hour:02d}",
                "start_minute": f"{t0.minute:02d}",
                "start_second": f"{t0.second:02d}",
                "end_year":     f"{t1.year:04d}",
                "end_month":    f"{t1.month:02d}",
                "end_day":      f"{t1.day:02d}",
                "end_hour":     f"{t1.hour:02d}",
                "end_minute":   f"{t1.minute:02d}",
                "end_second":   f"{t1.second:02d}",
            })

        # User overrides always win.
        patches.update(self.namelist_overrides)

        text = path.read_text()
        for key, value in patches.items():
            text = _patch_namelist_value(text, key, value)
        path.write_text(text)

        return fire_tign_in_time

    def _make_wrfinput(self, stage: Path, grid) -> None:
        """Produce ``wrfinput_d01.nc`` for the staged namelist.

        Real-data path is selected when both ``real_cmd`` and
        ``met_em_dir`` are configured; cube wind is irrelevant
        (real.exe gets the atmosphere from met_em). Otherwise fall back
        to ideal.exe, or — in test mode where neither is available —
        build a minimal NetCDF shell with the required dimensions so
        the later TIGN_IN/NFUEL_CAT/ZSF surgery has something to write
        into.
        """
        wrfinput = stage / "wrfinput_d01.nc"

        if self.real_cmd is not None and self.met_em_dir is not None:
            n_met = self._stage_met_em_files(stage)
            if not n_met:
                raise FileNotFoundError(
                    f"no met_em.d01* files found in {self.met_em_dir}")
            _log.info("[wrf_sfire] staged %d met_em files for real.exe",
                       n_met)
            subprocess.run(self._cmd_for_stage(self.real_cmd),
                           cwd=stage, check=True)
        elif self.ideal_cmd is not None:
            subprocess.run(self._cmd_for_stage(self.ideal_cmd),
                           cwd=stage, check=True)

        if not wrfinput.exists():
            # Test-mode fallback: build a minimal wrfinput shell so
            # _inject_sfire_inputs has something to write into.
            self._write_minimal_wrfinput(wrfinput, grid)

    def _cmd_for_stage(self, cmd: list[str]) -> list[str]:
        """Resolve `./binary` commands against sfire_dir for temp stages.

        The adapter runs from a fresh stage directory, so relative
        commands such as ``wrf-sfire/main/wrf.exe`` would otherwise be
        resolved relative to that stage. If the command exists relative
        to the current project directory or under sfire_dir, run that
        absolute path while keeping cwd=stage for model I/O.
        """
        if not cmd:
            return cmd
        out = list(cmd)
        exe = Path(out[0])
        if not exe.is_absolute():
            for candidate in (exe.resolve(), (self.sfire_dir / exe).resolve()):
                if candidate.exists():
                    out[0] = str(candidate)
                    break
        return out

    def _stage_met_em_files(self, stage: Path) -> int:
        if self.met_em_dir is None:
            return 0
        if not self.met_em_dir.exists():
            raise FileNotFoundError(self.met_em_dir)
        files = sorted(self.met_em_dir.glob("met_em.d01*"))
        for src in files:
            dest = stage / src.name
            if dest.exists():
                continue
            try:
                dest.symlink_to(src.resolve())
            except OSError:
                shutil.copy2(src, dest)
        return len(files)

    def _patch_input_sounding_with_wind(self, sounding_path: Path,
                                         inputs: dict[str, Any],
                                         request: Request) -> bool:
        """Overwrite the u/v columns of every per-level row in
        ``input_sounding`` with cube wind at scenario start. Returns
        True when at least one row was patched.

        Only relevant for the ideal-fallback path. real.exe ignores
        ``input_sounding`` entirely (the atmosphere comes from met_em).
        Cheap enough to do unconditionally.
        """
        wind_speed = inputs.get("wind_speed_ms")
        wind_dir = inputs.get("wind_dir_deg")
        if wind_speed is None or wind_dir is None:
            return False
        if not sounding_path.exists():
            return False

        ts_s, speed_arr = wind_speed
        ts_d, dir_arr = wind_dir
        if list(ts_s) != list(ts_d):
            raise RuntimeError(
                "wind_speed_ms and wind_dir_deg time axes differ; "
                "re-fetch wind so both share a single time axis")
        if request.t_start is None:
            return False

        # Closest hour to scenario start.
        t_target = request.t_start.replace(
            minute=0, second=0, microsecond=0)
        deltas = [abs((t - t_target).total_seconds()) for t in ts_s]
        ti = int(np.argmin(deltas))

        # Cube-centre cell — for the small AOIs the asteroid scenarios
        # use, the 10 m wind is effectively uniform; the centre is a
        # reproducible representative.
        H, W = speed_arr.shape[1], speed_arr.shape[2]
        cy, cx = H // 2, W // 2
        speed = float(speed_arr[ti, cy, cx])
        direction = float(dir_arr[ti, cy, cx])

        # Meteorological direction (wind comes FROM) -> earth-frame u/v.
        # u positive = blows east; v positive = blows north.
        dir_rad = float(np.deg2rad(direction))
        u_earth = -speed * float(np.sin(dir_rad))
        v_earth = -speed * float(np.cos(dir_rad))

        old = sounding_path.read_text().splitlines()
        if not old:
            return False
        new_lines = [old[0]]              # surface line untouched
        patched = False
        for line in old[1:]:
            tokens = line.split()
            if len(tokens) < 5:
                new_lines.append(line)     # malformed; leave alone
                continue
            height, theta, qv = tokens[0], tokens[1], tokens[2]
            new_lines.append(
                f" {float(height):11.2f} {float(theta):11.2f} "
                f"{float(qv):11.2f} {u_earth:11.3f} {v_earth:11.3f}"
            )
            patched = True
        if not patched:
            return False
        sounding_path.write_text("\n".join(new_lines) + "\n")
        return True

    def _write_minimal_wrfinput(self, path: Path, grid) -> None:
        """Create a NetCDF shell with the dimensions SFIRE expects,
        used in test mode when no real ideal.exe is available."""
        import netCDF4 as nc
        H, W = grid.shape
        Hf, Wf = H * self.fmr, W * self.fmr
        with nc.Dataset(path, "w", format="NETCDF4") as ds:
            ds.createDimension("Time", 1)
            ds.createDimension("south_north", H)
            ds.createDimension("west_east",   W)
            ds.createDimension("south_north_subgrid", Hf)
            ds.createDimension("west_east_subgrid",   Wf)

    def _inject_sfire_inputs(self, wrfinput: Path, grid,
                              inputs: dict[str, Any],
                              fire_tign_in_time: float) -> None:
        """Overwrite TIGN_IN / NFUEL_CAT / ZSF on the fire mesh in
        ``wrfinput_d01.nc`` with cube-derived values.

        A cell is "pre-ignited" iff (1) ignition_t0 is finite + non-
        negative AND (2) nfuel_cat is a burnable Anderson 13 code
        (1..13). Non-burnable codes (LANDFIRE 91/92/93/98/99 mapped to
        14 by drivers.landfire_fbfm13) are masked out so urban / snow
        / water cells don't get flagged as pre-ignited just because
        the asteroid thermal pulse reached them.

        ZSF is sampled at fire-mesh resolution from ``fire_dem_driver``
        when available; otherwise the atmosphere DEM is nearest-
        neighbour upsampled.
        """
        import netCDF4 as nc
        H, W = grid.shape
        Hf, Wf = H * self.fmr, W * self.fmr

        ign_t0 = np.asarray(inputs["ignition_t0"], dtype="float32")
        nfuel_atm = np.asarray(inputs["nfuel_cat"], dtype="float32")
        dem_atm = np.asarray(inputs["dem"], dtype="float32")

        burnable = (nfuel_atm >= 1.0) & (nfuel_atm <= 13.0)
        valid = np.isfinite(ign_t0) & (ign_t0 >= 0) & burnable
        tign_atm = np.full_like(ign_t0, _UNIGNITED_SENTINEL_S,
                                 dtype="float32")
        tign_atm[valid] = np.minimum(ign_t0[valid],
                                      fire_tign_in_time - 0.1)

        tign_fire = _block_replicate(tign_atm, (self.fmr, self.fmr))
        nfuel_fire = _block_replicate(nfuel_atm, (self.fmr, self.fmr))
        zsf_fire = self._build_zsf_fire(grid, dem_atm)

        fields = (
            ("TIGN_IN",   tign_fire,  "s",
             "Per-cell ignition time (asteroid pulse + sentinel for "
             "un-ignited cells)"),
            ("NFUEL_CAT", nfuel_fire, "",
             "Anderson 13 fuel category"),
            ("ZSF",       zsf_fire,   "m",
             "Terrain height on fire mesh"),
        )
        with nc.Dataset(wrfinput, "a") as ds:
            self._ensure_fire_dims(ds, Hf, Wf)
            for name, arr, units, desc in fields:
                self._upsert_fire_var(ds, name, arr,
                                       units=units, description=desc)

    def _build_zsf_fire(self, grid, dem_atm: np.ndarray) -> np.ndarray:
        """ZSF on the fire mesh.

        Preferred path: sample the DEM at the fire mesh resolution via
        ``fire_dem_driver.fetch_to_array`` so each 90 m cell has its own
        elevation. Fallback: nearest-neighbour upsample of the cube DEM
        (every 100 fire cells share the underlying 900 m value).
        """
        H, W = grid.shape
        if self.fire_dem_driver is None:
            return _block_replicate(dem_atm, (self.fmr, self.fmr))

        Hf, Wf = H * self.fmr, W * self.fmr
        xmin = grid.x0
        xmax = grid.x0 + W * grid.pixel_m
        ymax = grid.y1
        ymin = grid.y1 - H * grid.pixel_m
        _log.info("[wrf_sfire] sampling fire-mesh ZSF at %d x %d via %s",
                   Hf, Wf, getattr(self.fire_dem_driver, "name",
                                    type(self.fire_dem_driver).__name__))
        zsf = self.fire_dem_driver.fetch_to_array(
            xmin, ymin, xmax, ymax, Wf, Hf, int(grid.crs_epsg))
        return zsf.astype("float32")

    @staticmethod
    def _ensure_fire_dims(ds, Hf: int, Wf: int) -> None:
        if "south_north_subgrid" not in ds.dimensions:
            ds.createDimension("south_north_subgrid", Hf)
        if "west_east_subgrid" not in ds.dimensions:
            ds.createDimension("west_east_subgrid", Wf)

    @staticmethod
    def _upsert_fire_var(ds, name: str, array: np.ndarray, *,
                          units: str = "",
                          description: str = "") -> None:
        dims = ("south_north_subgrid", "west_east_subgrid")
        if name in ds.variables:
            ds.variables[name][:] = array
        else:
            var = ds.createVariable(name, "f4", dims)
            var[:] = array
            if units:
                var.units = units
            if description:
                var.description = description

    @staticmethod
    def _read_last_frame(ds, name: str) -> np.ndarray:
        """Read the last time slice of a fire-mesh variable
        (Time, sn, we) or return the array as-is if it's only 2D."""
        var = ds.variables[name]
        arr = np.asarray(var[:])
        if arr.ndim == 3:
            return arr[-1]
        return arr


# ====================================================================
# small utilities
# ====================================================================
_NAMELIST_KEY_RE = re.compile(
    r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)([^,\n!]+?)(\s*[,]?\s*)(![^\n]*)?$",
    re.MULTILINE)


def _patch_namelist_value(text: str, key: str, value: str) -> str:
    """Replace every occurrence of ``key = ...`` in a Fortran namelist.

    Preserves whitespace and trailing comments. Only PATCHES existing
    keys — relies on the template carrying every key the producer
    needs. Multiple matches all get the same value (a well-formed
    namelist has at most one definition per key per section)."""
    def repl(m):
        if m.group(2).lower() != key.lower():
            return m.group(0)
        return (f"{m.group(1)}{m.group(2)}{m.group(3)}{value}"
                f"{m.group(5)}{m.group(6) or ''}")

    return _NAMELIST_KEY_RE.sub(repl, text)


def _block_replicate(arr: np.ndarray, factor: tuple[int, int]) -> np.ndarray:
    """Upsample by integer factor via nearest-neighbour replication."""
    fy, fx = factor
    return np.repeat(np.repeat(arr, fy, axis=0), fx, axis=1)


def _block_reduce(arr: np.ndarray, factor: tuple[int, int],
                   *, op: str = "mean") -> np.ndarray:
    """Downsample by block reduction. `op` is 'mean' or 'min'."""
    fy, fx = factor
    H, W = arr.shape
    Hc, Wc = (H // fy) * fy, (W // fx) * fx
    trimmed = arr[:Hc, :Wc].reshape(Hc // fy, fy, Wc // fx, fx)
    if op == "min":
        return trimmed.min(axis=(1, 3))
    if op == "max":
        return trimmed.max(axis=(1, 3))
    return trimmed.mean(axis=(1, 3))
