"""Adapter that calls the external WRF-SFIRE model from the engine.

The MODEL is the external Fortran WRF-SFIRE checkout. This file is just
the ADAPTER: Python wiring that stages cube data into SFIRE's expected
NetCDF/namelist format, subprocesses the `real.exe` / `wrf.exe`
binaries, and parses the wrfout back into the cube. No fire physics
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
  * wind_speed_ms / wind_dir_deg - optional canonical cube wind time series.
                    If both cover the requested run period, the adapter
                    translates them into WRF-SFIRE's sounding format. The
                    real.exe path also requires prebuilt WRF-native
                    met_em.d01.* atmospheric inputs.

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

External setup (one-time, outside the adapter):
    # WRF-SFIRE supplies real.exe / wrf.exe / ideal.exe.
    git clone https://github.com/openwfm/wrf-sfire wrf-sfire
    cd wrf-sfire
    module load gcc netcdf-c netcdf-fortran
    ./configure
    ./compile em_fire 2>&1 | tee compile_em_fire.log
    ./compile em_real 2>&1 | tee compile_em_real.log
    # main/ should now contain wrf.exe, ideal.exe, and real.exe.

    # WPS supplies geogrid.exe / ungrib.exe / metgrid.exe. Run it outside
    # this adapter to produce met_em.d01.* files for the requested domain.
    cd ..
    git clone https://github.com/openwfm/WPS WPS
    cd WPS
    export WRF_DIR="$(pwd)/../wrf-sfire"
    ./configure
    ./compile 2>&1 | tee compile_wps.log

    # WPS_GEOG is static land-use/elevation/soil data used by geogrid.exe.
    # It is not read by this adapter directly, but WPS needs it before it
    # can create geo_em/met_em files for a real-data run.
    cd ..
    wget https://demo.openwfm.org/web/wrfx/WPS_GEOG.tbz
    tar xvfj WPS_GEOG.tbz

Runtime contract:
    * WPS_GEOG is used by WPS/geogrid, not by WRFSFireAdapter.
    * WPS/metgrid produces met_em.d01.* files in `met_em_dir`.
    * WRFSFireAdapter stages those WRF-native files, runs real.exe,
      injects cube-derived fire fields, runs wrf.exe, and writes outputs
      back to the cube.

About WRFx:
    wrfxpy / wrfxweb / wrfxctrl are OpenWFM's optional orchestration,
    visualization, and web-submission stack. This repository already has its
    own engine, cube, drivers, and backends, so WRFSFireAdapter does not need
    wrfxpy, WRFx queue templates, or WRFx tokens. Use WRFx only if you want
    its full forecasting/web workflow instead of this engine. Tokens such as
    MesoWest or Earthdata are only needed by the data-acquisition tool that
    contacts those services; the current ERA5/LANDFIRE/DEM drivers here do
    not use WRFx token files.

Usage:
    from models.wrf_sfire_adapter import WRFSFireAdapter
    from engine import to_engine_registry, Pipeline, PipelineRunner

    adapter = WRFSFireAdapter(
        sfire_dir="wrf-sfire/test/em_fire/hill",
        ideal_cmd=["wrf-sfire/main/ideal.exe"],
        real_cmd=["wrf-sfire/main/real.exe"],
        met_em_dir="data/wps_runs/run_001/met_em",
        wrf_cmd=["wrf-sfire/main/wrf.exe"],
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
                 stage_root: str | Path | None = None,
                 keep_stage: bool = False) -> None:
        """
        sfire_dir : directory holding the WRF-SFIRE template files
                     (namelist.input, namelist.fire, input_sounding,
                      and the wrf.exe/ideal.exe binaries when present)
        ideal_cmd : command to run ideal.exe. None skips ideal (test mode);
                     the producer then builds wrfinput from scratch unless
                     real_cmd is selected.
        real_cmd  : command to run real.exe when complete cube wind inputs
                     and prebuilt WRF-native met_em inputs are available.
                     None disables real.exe and keeps the ideal/minimal path.
        wrf_cmd   : command to run wrf.exe (e.g. ["./wrf.exe"]).
        sim_seconds : total fire-spread simulation length in seconds.
        history_interval_s : how often SFIRE writes a wrfout history frame.
        fire_mesh_ratio : SFIRE's fire mesh refinement factor over the
                          atmosphere mesh (typically 4).
        namelist_template : template namelist.input in sfire_dir. The
                            shipped 'namelist.input_ignite_from_tign_in'
                            is the canonical TIGN_IN-driven scenario.
        met_em_dir : optional directory of prebuilt WPS `met_em.d01.*`
                     files. When present, they are symlinked/copied into
                     the stage before real.exe runs.
        namelist_overrides : extra `key=value` lines to inject into
                              namelist.input after the standard patches.
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
        if stage_root is not None:
            self.stage_root = Path(stage_root)
        self.keep_stage = bool(keep_stage)

    # ============================================================ BOOTSTRAP
    @classmethod
    def bootstrap(cls,
                   install_root: str | Path = "wrf-sfire-stack",
                   *,
                   dry_run: bool = False,
                   **kwargs):
        """Provision WRF-SFIRE + WPS + WPS_GEOG under `install_root`.

        Thin pass-through to `models.wrf_sfire_bootstrap.bootstrap_wrf_sfire_stack`.
        Keeps callers from importing the bootstrap module directly when
        the adapter is the obvious entry point.

        Returns the `BootstrapPlan` describing what was (or, if
        `dry_run=True`, what would be) done. After a successful real
        bootstrap, instantiate the adapter with:

            adapter = WRFSFireAdapter(
                sfire_dir=plan.wrf_sfire_dir / "test/em_fire/hill",
                ideal_cmd=[str(plan.wrf_sfire_dir / "main/ideal.exe")],
                real_cmd =[str(plan.wrf_sfire_dir / "main/real.exe")],
                wrf_cmd  =[str(plan.wrf_sfire_dir / "main/wrf.exe")],
                met_em_dir=...,
            )
        """
        from .wrf_sfire_bootstrap import bootstrap_wrf_sfire_stack
        return bootstrap_wrf_sfire_stack(
            install_root, dry_run=dry_run, **kwargs)

    # ============================================================ STAGE
    def stage_inputs(self, grid, inputs: dict[str, Any],
                     request: Request, stage: Path) -> None:
        """Build a complete WRF-SFIRE stage dir:

          1. Copy template namelist.input / namelist.fire / input_sounding
          1b. If cube wind is available, overwrite input_sounding's per-
              level u/v columns with real wind at scenario start (so
              the ideal fallback initialises with real meteorology, not
              template wind).
          2. Patch namelist.input with grid dims, time, fire_tign_in_time
          3. If complete cube wind + real_cmd + prebuilt WRF-native met_em
             inputs are available, stage met_em and run real.exe; otherwise
             run ideal.exe (or skip and create a minimal wrfinput in test
             mode).
          4. Inject TIGN_IN / NFUEL_CAT / ZSF into wrfinput_d01.nc
        """
        H, W = grid.shape
        wind_state = self._cube_wind_for_required_period(inputs, request)

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

        # 1b. Overwrite input_sounding u/v with cube wind when present.
        # Even when real.exe is unavailable, the ideal fallback now starts
        # from the scenario wind instead of the template's canned wind.
        used_real_wind = self._patch_input_sounding_with_wind(
            stage / "input_sounding", inputs, request)
        if used_real_wind:
            print(f"[wrf_sfire] input_sounding patched with cube wind at "
                  f"{request.t_start}")
        else:
            print("[wrf_sfire] no usable cube wind; using template sounding")

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

        # 3. Run real.exe only when WRF-native atmospheric inputs are
        # available. Wind speed/direction are cube variables; met_em files
        # are the WRF-required atmospheric input format.
        real_requested = self.real_cmd is not None
        real_ready = (
            wind_state is not None
            and self.real_cmd is not None
            and self.met_em_dir is not None
        )
        if real_ready:
            n_met = self._stage_met_em_files(stage)
            if not n_met:
                raise FileNotFoundError(
                    f"no met_em.d01* files found in {self.met_em_dir}")
            print(f"[wrf_sfire] staged {n_met} met_em files for real.exe")
            print("[wrf_sfire] using real.exe path with cube wind coverage")
            subprocess.run(self._cmd_for_stage(self.real_cmd),
                           cwd=stage, check=True)
        else:
            if real_requested and wind_state is None:
                print("[wrf_sfire] cube wind does not cover requested "
                      "period; using ideal/minimal path")
            elif real_requested and self.met_em_dir is None:
                print("[wrf_sfire] cube wind is available, but no "
                      "met_em_dir is configured; using ideal/minimal path")
        if not real_ready and self.ideal_cmd is not None:
            subprocess.run(self._cmd_for_stage(self.ideal_cmd),
                           cwd=stage, check=True)
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
    def _cmd_for_stage(self, cmd: list[str]) -> list[str]:
        """Resolve `./binary` commands against sfire_dir for temp stages.

        The adapter runs from a fresh stage directory, so relative commands
        such as `wrf-sfire/main/wrf.exe` would otherwise be resolved relative
        to that stage. If the command exists relative to the current project
        directory or under sfire_dir, run that absolute path while keeping
        cwd=stage for model I/O.
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

    def _cube_wind_for_required_period(self, inputs: dict[str, Any],
                                        request: Request
                                        ) -> Optional[dict[str, Any]]:
        """Return cube wind inputs only when both variables cover the run.

        Coverage is checked at hourly cadence because the current wind
        drivers write hourly fields. A 10-minute run starting at 12:00 only
        requires the 12:00 field; a three-hour run requires 12:00, 13:00,
        and 14:00.
        """
        wind_speed = inputs.get("wind_speed_ms")
        wind_dir = inputs.get("wind_dir_deg")
        if wind_speed is None or wind_dir is None:
            return None
        if request.t_start is None:
            return None

        ts_s, speed_arr = wind_speed
        ts_d, dir_arr = wind_dir
        if list(ts_s) != list(ts_d):
            raise RuntimeError(
                "wind_speed_ms and wind_dir_deg time axes differ; "
                "re-fetch wind so both share a single time axis")
        if len(ts_s) == 0:
            return None

        t0, t1 = self._required_wind_window(request)
        available = {self._floor_hour(t) for t in ts_s}
        t = t0
        while t <= t1:
            if t not in available:
                return None
            t += timedelta(hours=1)

        return {
            "ts": list(ts_s),
            "speed": np.asarray(speed_arr, dtype="float32"),
            "direction": np.asarray(dir_arr, dtype="float32"),
            "required_start": t0,
            "required_end": t1,
        }

    def _required_wind_window(self, request: Request):
        start = self._floor_hour(request.t_start)
        if request.t_end is not None:
            end_raw = request.t_end
        else:
            end_raw = request.t_start + timedelta(seconds=self.sim_seconds)
        if end_raw <= request.t_start:
            end_raw = request.t_start
        # Request end is exclusive. Use the final covered hour.
        end = self._floor_hour(end_raw - timedelta(microseconds=1))
        if end < start:
            end = start
        return start, end

    @staticmethod
    def _floor_hour(t):
        return t.replace(minute=0, second=0, microsecond=0)

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
        """If `inputs` carries cube wind, replace the u/v columns of every
        per-level row in `input_sounding` with real wind at scenario start.
        Returns True if at least one sounding row was patched.

        The template's surface line + per-level (height, theta, qv) are
        left intact; only the trailing u/v columns are swapped. The wind
        is broadcast vertically (10 m surface wind applied at every
        level). For more nuanced vertical profiles, fetch multi-level
        winds and extend this method.
        """
        wind_speed = inputs.get("wind_speed_ms")
        wind_dir = inputs.get("wind_dir_deg")
        if wind_speed is None or wind_dir is None:
            return False
        if not sounding_path.exists():
            return False

        ts_s, speed_arr = wind_speed       # (timestamps, (T, H, W))
        ts_d, dir_arr = wind_dir
        if list(ts_s) != list(ts_d):
            raise RuntimeError(
                "wind_speed_ms and wind_dir_deg time axes differ; "
                "re-fetch wind so both share a single time axis")
        if request.t_start is None:
            return False

        # Pick the timestamp closest to scenario start.
        t_target = request.t_start.replace(
            minute=0, second=0, microsecond=0)
        deltas = [abs((t - t_target).total_seconds()) for t in ts_s]
        ti = int(np.argmin(deltas))

        # Use the cube-center cell. For small AOIs the wind is ~uniform;
        # the center is a reproducible representative.
        H, W = speed_arr.shape[1], speed_arr.shape[2]
        cy, cx = H // 2, W // 2
        speed = float(speed_arr[ti, cy, cx])
        direction = float(dir_arr[ti, cy, cx])

        # Meteorological direction (wind comes FROM) -> earth-frame u/v.
        # u positive = blows east; v positive = blows north.
        dir_rad = float(np.deg2rad(direction))
        u_earth = -speed * float(np.sin(dir_rad))
        v_earth = -speed * float(np.cos(dir_rad))

        # Patch per-level rows: keep height, theta, qv; rewrite u, v.
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
        #
        #   1. ignition_t0 is finite + non-negative      (asteroid pulse
        #                                                  reached it), AND
        #   2. nfuel_cat is a burnable Anderson 13 code  (1..13). LANDFIRE
        #      non-burnable codes (91/92/93/98/99) are mapped to the
        #      no-fuel sentinel (14) by drivers.landfire_fbfm13, and the
        #      same gate covers any source that uses category 14 for "no
        #      fuel". This stops us flagging urban/snow/water cells as
        #      pre-ignited just because they sit inside the asteroid's
        #      thermal annulus.
        ign_t0 = np.asarray(inputs["ignition_t0"], dtype="float32")
        nfuel_atm = np.asarray(inputs["nfuel_cat"], dtype="float32")
        burnable_cell = (nfuel_atm >= 1.0) & (nfuel_atm <= 13.0)
        valid = (np.isfinite(ign_t0) & (ign_t0 >= 0) & burnable_cell)
        tign_atm = np.where(valid, ign_t0, _UNIGNITED_SENTINEL_S
                             ).astype("float32")
        # Cap pre-ignition times to fire_tign_in_time - epsilon so SFIRE
        # treats them as inside the pre-ignition window. (Edge guard.)
        tign_atm = np.where(
            tign_atm < _UNIGNITED_SENTINEL_S,
            np.minimum(tign_atm, fire_tign_in_time - 0.1),
            tign_atm)
        tign_fire = _block_replicate(tign_atm, (self.fmr, self.fmr))

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
