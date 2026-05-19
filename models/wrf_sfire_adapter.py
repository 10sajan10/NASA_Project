"""Adapter that calls the external WRF-SFIRE model from the engine.

The MODEL is the Fortran code in the cloned `wrf-sfire/` directory.
This file is just the ADAPTER: Python wiring that stages cube data into
SFIRE's expected NetCDF/namelist format, subprocesses the `wrf.exe`
binary, and parses the wrfout back into the cube. No fire physics
lives here.

If you swap WRF-SFIRE for a different external model, write a new
adapter file alongside this one; the engine substrate is unchanged.

End-to-end fire spread from an asteroid ignition pattern:
the engine resolves `ignition_t0` + fuels + terrain (+ optional weather)
from the cube; this adapter stages them into WRF-SFIRE's expected NetCDF
+ namelist format, runs the SFIRE binary, parses TIGN_G / FIRE_AREA
back into the cube as `arrival_s` and `fire_area`.

Inputs consumed (declared via DataAdapter):
  * ignition_t0  - per-cell time the asteroid pulse ignited the cell (s)
                    cells with NaN/<0 are taken to be un-ignited
  * nfuel_cat    - Anderson 13 fuel categories (1..13 burnable, 14 no-fuel)
  * dem          - surface elevation (m)
  * burnable     - optional; mask of cells that can burn
  * wind_speed_ms / wind_dir_deg - optional time series; if absent, the
                    namelist's static wind values are used

Outputs produced:
  * arrival_s    - fire arrival time per cell (TIGN_G), merged MONOTONE_MIN
  * fire_area    - cell-wise burned fraction (FIRE_AREA), merged MONOTONE_MAX

How SFIRE knows about the asteroid pulse:
  * The wrfinput NetCDF carries a TIGN_IN array on the fire mesh.
  * The namelist sets `fire_tign_in_time > 0`, which tells SFIRE to read
    TIGN_IN and treat every cell with `TIGN_IN < fire_tign_in_time` as
    pre-ignited at that time.
  * Cells outside the asteroid footprint get a large sentinel
    (TIGN_IN >> fire_tign_in_time) so SFIRE leaves them un-ignited and
    lets its physics propagate fire to them.

Building the binary (one-time, ~1 hour on CHPC):
    module load gcc netcdf-c netcdf-fortran
    cd wrf-sfire
    ./configure                  # pick gfortran serial (32)
    ./compile em_fire 2>&1 | tee build.log
    # produces wrf.exe + ideal.exe in main/

Usage:
    from models.wrf_sfire_adapter import WRFSFireAdapter
    from engine import to_engine_registry, Pipeline, PipelineRunner

    adapter = WRFSFireAdapter(
        sfire_dir="wrf-sfire/test/em_fire/hill",
        ideal_cmd=["./ideal.exe"],
        wrf_cmd=["./wrf.exe"],
        sim_seconds=3 * 3600,
        fire_mesh_ratio=4,
    )
    reg = to_engine_registry([adapter, ...upstream producers...])
    runner = PipelineRunner(reg)
    runner.run(cube, Pipeline.from_targets(["arrival_s"], registry=reg))

Test mode (no binaries built):
    Pass ``ideal_cmd=None`` and point ``wrf_cmd`` at a Python script that
    fakes the SFIRE outputs. The adapter's staging logic still runs, so
    the end-to-end data flow is validated. See
    tests/test_wrf_sfire_adapter.py.
"""
from __future__ import annotations

import re
import shutil
import subprocess
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
from engine.model_adapter import ModelAdapter


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
                  description="Optional wind speed series; "
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
                 wrf_cmd: list[str] | None = None,
                 sim_seconds: int = 3 * 3600,
                 history_interval_s: int = 120,
                 fire_mesh_ratio: int = 4,
                 namelist_template: str = "namelist.input_ignite_from_tign_in",
                 fire_namelist: str = "namelist.fire",
                 sounding: str = "input_sounding",
                 namelist_overrides: Optional[dict[str, str]] = None,
                 stage_root: str | Path | None = None,
                 keep_stage: bool = False) -> None:
        """
        sfire_dir : directory holding the WRF-SFIRE template files
                     (namelist.input, namelist.fire, input_sounding,
                      and the wrf.exe/ideal.exe binaries when present)
        ideal_cmd : command to run ideal.exe. None skips ideal (test mode);
                     the producer then builds wrfinput from scratch.
        wrf_cmd   : command to run wrf.exe (e.g. ["./wrf.exe"]).
        sim_seconds : total fire-spread simulation length in seconds.
        history_interval_s : how often SFIRE writes a wrfout history frame.
        fire_mesh_ratio : SFIRE's fire mesh refinement factor over the
                          atmosphere mesh (typically 4).
        namelist_template : template namelist.input in sfire_dir. The
                            shipped 'namelist.input_ignite_from_tign_in'
                            is the canonical TIGN_IN-driven scenario.
        namelist_overrides : extra `key=value` lines to inject into
                              namelist.input after the standard patches.
        """
        self.sfire_dir = Path(sfire_dir)
        self.ideal_cmd = ideal_cmd
        self.wrf_cmd = wrf_cmd or ["./wrf.exe"]
        self.sim_seconds = int(sim_seconds)
        self.history_interval_s = int(history_interval_s)
        self.fmr = int(fire_mesh_ratio)
        self.namelist_template = namelist_template
        self.fire_namelist = fire_namelist
        self.sounding = sounding
        self.namelist_overrides = dict(namelist_overrides or {})
        if stage_root is not None:
            self.stage_root = Path(stage_root)
        self.keep_stage = bool(keep_stage)

    # ============================================================ STAGE
    def stage_inputs(self, grid, inputs: dict[str, Any],
                     request: Request, stage: Path) -> None:
        """Build a complete WRF-SFIRE stage dir:

          1. Copy template namelist.input / namelist.fire / input_sounding
          2. Patch namelist.input with grid dims, time, fire_tign_in_time
          3. Run ideal.exe (or skip and create a minimal wrfinput)
          4. Inject TIGN_IN / NFUEL_CAT / ZSF into wrfinput_d01.nc
        """
        H, W = grid.shape

        # 1. Copy templates ------------------------------------------------
        for filename in (self.namelist_template,
                          self.fire_namelist,
                          self.sounding):
            src = self.sfire_dir / filename
            if not src.exists():
                raise FileNotFoundError(
                    f"WRF-SFIRE template missing: {src}")
        shutil.copy(self.sfire_dir / self.namelist_template,
                     stage / "namelist.input")
        shutil.copy(self.sfire_dir / self.fire_namelist,
                     stage / "namelist.fire")
        shutil.copy(self.sfire_dir / self.sounding, stage / "input_sounding")

        # 2. Patch namelist.input -----------------------------------------
        ign_t0 = np.asarray(inputs["ignition_t0"], dtype="float32")
        finite_ign = ign_t0[np.isfinite(ign_t0) & (ign_t0 >= 0)]
        max_ign_s = float(finite_ign.max()) if finite_ign.size else 0.0
        # SFIRE pre-ignites cells with TIGN_IN < fire_tign_in_time.
        # Add a 1-second margin so the latest asteroid ignition is included.
        fire_tign_in_time = max_ign_s + 1.0

        patches = {
            "run_seconds": str(self.sim_seconds),
            "run_minutes": "0",
            "run_hours":   "0",
            "run_days":    "0",
            "end_second":  str(self.sim_seconds % 60),
            "end_minute":  str((self.sim_seconds // 60) % 60),
            "end_hour":    str(self.sim_seconds // 3600),
            "history_interval_s": str(self.history_interval_s),
            "e_we":        str(W + 1),       # WRF e_we = nx + 1
            "e_sn":        str(H + 1),
            "dx":          str(float(grid.pixel_m)),
            "dy":          str(float(grid.pixel_m)),
            "sr_x":        str(self.fmr),
            "sr_y":        str(self.fmr),
            "fire_tign_in_time": f"{fire_tign_in_time:.3f}",
            "fire_num_ignitions": "0",       # no point-source ignitions
        }
        patches.update(self.namelist_overrides)
        text = (stage / "namelist.input").read_text()
        for key, value in patches.items():
            text = _patch_namelist_value(text, key, value)
        (stage / "namelist.input").write_text(text)

        # 3. Run ideal.exe (or skip and create a minimal wrfinput) --------
        if self.ideal_cmd is not None:
            subprocess.run(self.ideal_cmd, cwd=stage, check=True)
        wrfinput = stage / "wrfinput_d01.nc"
        if not wrfinput.exists():
            # Test-mode fallback: build a minimal wrfinput shell so the
            # NetCDF patch below has something to write into.
            self._write_minimal_wrfinput(wrfinput, grid)

        # 4. Inject the asteroid ignition + fuels + terrain --------------
        self._inject_sfire_inputs(wrfinput, grid, inputs,
                                   fire_tign_in_time)

    # ============================================================ RUN
    def run_model(self, stage: Path, request: Request) -> Path:
        subprocess.run(self.wrf_cmd, cwd=stage, check=True)
        return stage

    # ============================================================ PARSE
    def parse_outputs(self, output_path: Path,
                      grid) -> dict[str, Any]:
        wrfouts = sorted(output_path.glob("wrfout_d01_*"))
        if not wrfouts:
            raise FileNotFoundError(
                f"no wrfout_d01_* found in {output_path}")
        # Latest output frame holds the final fire state.
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

        # Mark un-ignited cells (SFIRE writes a large sentinel for those)
        # as NaN so downstream consumers don't treat them as arrival t=1e9.
        arrival_s = np.where(arrival_s > 0.99 * _UNIGNITED_SENTINEL_S,
                              np.nan, arrival_s).astype("float32")
        return {
            "arrival_s": arrival_s,
            "fire_area": fire_area.astype("float32"),
        }

    # ============================================================ helpers
    def _write_minimal_wrfinput(self, path: Path, grid) -> None:
        """Create a NetCDF shell with the dimensions SFIRE expects, used
        in test mode when no real ideal.exe is available."""
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
        wrfinput_d01.nc with cube-derived values."""
        import netCDF4 as nc
        H, W = grid.shape
        Hf, Wf = H * self.fmr, W * self.fmr

        # Build the fire-mesh TIGN_IN field. A cell is "pre-ignited" iff
        # ignition_t0 is finite + non-negative. Whether the cell can
        # actually carry fire is `nfuel_cat`'s job — no-fuel categories
        # are written there, not in a separate boolean mask.
        ign_t0 = np.asarray(inputs["ignition_t0"], dtype="float32")
        valid = np.isfinite(ign_t0) & (ign_t0 >= 0)
        tign_atm = np.where(valid, ign_t0, _UNIGNITED_SENTINEL_S
                             ).astype("float32")
        # Cap pre-ignition times to fire_tign_in_time - epsilon so SFIRE
        # treats them as inside the pre-ignition window. (Edge guard.)
        tign_atm = np.where(
            tign_atm < _UNIGNITED_SENTINEL_S,
            np.minimum(tign_atm, fire_tign_in_time - 0.1),
            tign_atm)
        tign_fire = _block_replicate(tign_atm, (self.fmr, self.fmr))

        nfuel_atm = np.asarray(inputs["nfuel_cat"], dtype="float32")
        nfuel_fire = _block_replicate(nfuel_atm, (self.fmr, self.fmr))

        dem_atm = np.asarray(inputs["dem"], dtype="float32")
        zsf_fire = _block_replicate(dem_atm, (self.fmr, self.fmr))

        with nc.Dataset(wrfinput, "a") as ds:
            self._ensure_fire_dims(ds, Hf, Wf)
            self._upsert_fire_var(ds, "TIGN_IN", tign_fire,
                                   units="s",
                                   description="Per-cell ignition time "
                                               "(asteroid pulse + sentinel "
                                               "for un-ignited cells)")
            self._upsert_fire_var(ds, "NFUEL_CAT", nfuel_fire,
                                   description="Anderson 13 fuel category")
            self._upsert_fire_var(ds, "ZSF", zsf_fire,
                                   units="m",
                                   description="Terrain heights, fire mesh")

    @staticmethod
    def _ensure_fire_dims(ds, Hf: int, Wf: int) -> None:
        """Make sure the fire-mesh dimensions exist on the dataset."""
        if "south_north_subgrid" not in ds.dimensions:
            ds.createDimension("south_north_subgrid", Hf)
        if "west_east_subgrid" not in ds.dimensions:
            ds.createDimension("west_east_subgrid", Wf)

    @staticmethod
    def _upsert_fire_var(ds, name: str, array: np.ndarray, *,
                          units: str = "",
                          description: str = "") -> None:
        """Create or overwrite a fire-mesh 2D variable on the dataset."""
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
        """Read the last time slice of a fire-mesh variable (Time, sn, we)
        or return the array as-is if it's only 2D."""
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
    """Replace every occurrence of `key = ...` in a Fortran namelist.

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
