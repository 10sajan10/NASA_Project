#!/usr/bin/env python3
"""Run the WRF-SFIRE fire prediction model for the Dallas asteroid scenario.

Wires every driver + the WRF-SFIRE adapter into the engine and lets the
PipelineRunner satisfy each declared input variable from whatever
producer can provide it.

Defaults match the scenario you asked for:

  AOI         Dallas TX (-96.797, 32.776), 50 km radius
  Atmosphere  900 m cube grid
  Fire mesh   90 m (fire_mesh_ratio = 10)
  Start       2025-01-01 00:00 UTC
  Sim length  24 hours of WRF-SFIRE
  Wind window 10 days of ERA5 cached in the cube
              (the WRF run only consumes the first sim_seconds; the
               extra hours are kept in the cube for later analysis)
  Fuels       LANDFIRE LF2024 FBFM13, downsampled 30 m -> 900 m
  Ignition    Dallas.kml thermal annulus (Critical -> Unsurvivable)

Lifecycle
---------
First time on a machine:

    python scripts/run_fire_dallas.py --bootstrap --dry-run

Reviews the install plan without doing anything heavy. Then:

    python scripts/run_fire_dallas.py --bootstrap

Clones + compiles WRF-SFIRE + WPS + downloads WPS_GEOG (~30 min).

After that:

    python scripts/run_fire_dallas.py             # ideal.exe path
    python scripts/run_fire_dallas.py --real \\
        --met-em-dir data/wps_runs/run_001/met_em # real.exe path

The ideal path patches WRF's input_sounding with cube wind and runs
ideal.exe; it does NOT need WPS met_em files. The real path uses
real.exe and requires prebuilt met_em.d01.* files (run WPS yourself or
add a prepare_met_em helper later).

Inspect what the engine will resolve without touching the network:

    python scripts/run_fire_dallas.py --dry-run

Honest gaps
-----------
* met_em files are NOT auto-generated. The bootstrap installs WPS but
  doesn't run ungrib/metgrid for a specific time window. Until we add
  a prepare_met_em(start, end, grib_dir) helper, run WPS yourself or
  use --real=False.
* This script does not (yet) submit to SLURM. WRF-SFIRE will run in
  whatever shell you start it from. For real HPC runs, wrap the wrf.exe
  invocation in your cluster's queue submission.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# --------------------------------------------------------------------- defaults
DEFAULT_LON = -96.797
DEFAULT_LAT = 32.776
DEFAULT_RADIUS_M = 50_000.0
DEFAULT_PIXEL_M = 900.0          # atmosphere mesh
DEFAULT_FIRE_MESH_RATIO = 10     # fire mesh = 90 m
DEFAULT_SIM_SECONDS = 24 * 3600  # 24 h WRF-SFIRE run
DEFAULT_WIND_DAYS = 10           # 10 days of ERA5 cached in cube
DEFAULT_INSTALL_ROOT = PROJECT_ROOT / "wrf-sfire-stack"
DEFAULT_OUT = PROJECT_ROOT / "data" / "runs" / "dallas_2025-01-01"
DEFAULT_KML = PROJECT_ROOT / "Dallas.kml"


# --------------------------------------------------------------------- CLI
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", default="2025-01-01T00:00",
                    help="Scenario start (UTC, ISO 8601). "
                         "Default: 2025-01-01T00:00")
    p.add_argument("--lon", type=float, default=DEFAULT_LON,
                    help=f"AOI center longitude. Default {DEFAULT_LON}")
    p.add_argument("--lat", type=float, default=DEFAULT_LAT,
                    help=f"AOI center latitude. Default {DEFAULT_LAT}")
    p.add_argument("--radius-m", type=float, default=DEFAULT_RADIUS_M,
                    help=f"AOI radius in metres. Default {DEFAULT_RADIUS_M}")
    p.add_argument("--pixel-m", type=float, default=DEFAULT_PIXEL_M,
                    help=f"Atmosphere mesh resolution. Default "
                         f"{DEFAULT_PIXEL_M}")
    p.add_argument("--fire-mesh-ratio", type=int,
                    default=DEFAULT_FIRE_MESH_RATIO,
                    help=f"Fire mesh refinement. Default "
                         f"{DEFAULT_FIRE_MESH_RATIO} -> "
                         f"{DEFAULT_PIXEL_M / DEFAULT_FIRE_MESH_RATIO:.0f} m "
                         "fire mesh")
    p.add_argument("--sim-seconds", type=int, default=DEFAULT_SIM_SECONDS,
                    help=f"Total WRF-SFIRE run length. Default "
                         f"{DEFAULT_SIM_SECONDS} s")
    p.add_argument("--wind-days", type=int, default=DEFAULT_WIND_DAYS,
                    help=f"Days of ERA5 wind to cache in the cube. "
                         f"Default {DEFAULT_WIND_DAYS}")
    p.add_argument("--kml", type=Path, default=DEFAULT_KML,
                    help=f"PDC thermal-damage KML. Default {DEFAULT_KML}")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"Cube root (Zarr + DuckDB). Default {DEFAULT_OUT}")
    p.add_argument("--install-root", type=Path,
                    default=DEFAULT_INSTALL_ROOT,
                    help="Where to install / find WRF-SFIRE + WPS + "
                         "WPS_GEOG. Default: project_root/wrf-sfire-stack")
    p.add_argument("--bootstrap", action="store_true",
                    help="Clone+compile WRF-SFIRE+WPS, download WPS_GEOG "
                         "before the run. Idempotent.")
    p.add_argument("--netcdf", default=None,
                    help="Value of $NETCDF for the bootstrap (a directory "
                         "with include/ + lib/). Required for WRF compile "
                         "if it isn't already in your shell environment.")
    p.add_argument("--hdf5", default=None,
                    help="Deprecated for the default bootstrap path; "
                         "WRF-SFIRE is built with NETCDF_classic=1, so "
                         "explicit HDF5/HD5 settings are ignored.")
    p.add_argument("--skip-wps", action="store_true",
                    help="Bootstrap: skip cloning/compiling WPS "
                         "(saves jasper dependency when only the ideal "
                         "path is needed).")
    p.add_argument("--skip-wps-geog", action="store_true",
                    help="Bootstrap: skip downloading WPS_GEOG.")
    p.add_argument("--skip-wrf", action="store_true",
                    help="Bootstrap: skip WRF-SFIRE itself.")
    p.add_argument("--real", action="store_true",
                    help="Use real.exe (requires prebuilt met_em files via "
                         "--met-em-dir). Default uses ideal.exe.")
    p.add_argument("--met-em-dir", type=Path, default=None,
                    help="Directory of prebuilt WPS met_em.d01.* files. "
                         "Only used when --real is passed.")
    p.add_argument("--dry-run", action="store_true",
                    help="Print plan + registry contents; do not touch "
                         "the network or run the model.")
    p.add_argument("--log-dir", type=Path,
                    default=PROJECT_ROOT / "logs",
                    help="Where to write rotating log + RunResult JSON. "
                         f"Default: project_root/logs")
    p.add_argument("--log-level", default="INFO",
                    choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                    help="Console log level. File always logs DEBUG.")
    return p.parse_args(argv)


# --------------------------------------------------------------------- helpers
def parse_dt(s: str) -> datetime:
    """ISO 8601 -> datetime. Accepts 'YYYY-MM-DDTHH:MM' and full ISO."""
    return datetime.fromisoformat(s)


def build_cube(out: Path, lon: float, lat: float,
                radius_m: float, pixel_m: float):
    """Create / open the cube on the requested grid."""
    from cube.grid import SimulationGrid
    from cube.store import Cube
    out.mkdir(parents=True, exist_ok=True)
    grid = SimulationGrid.from_center_radius(lon, lat, radius_m, pixel_m)
    return Cube(out, grid)


def build_registry(*, kml: Path, install_root: Path,
                    sim_seconds: int, fmr: int,
                    use_real: bool, met_em_dir: Path | None):
    """Register one driver per declared variable + the WRF-SFIRE adapter."""
    from engine import DataDriverAdapter, ProducerRegistry
    from drivers.thermal import ThermalDriver
    from drivers.landfire_fbfm13 import LandfireFBFM13Driver
    from drivers.dem import DEMDriver
    from drivers.era5_wind import ERA5WindDriver
    from models.wrf_sfire_adapter import WRFSFireAdapter

    sfire_dir = install_root / "WRF-SFIRE" / "test" / "em_fire" / "hill"
    bin_dir = install_root / "WRF-SFIRE" / "main"

    # Dedicated DEM driver instance for the adapter to sample ZSF at
    # the fire-mesh resolution (separate cache so it doesn't collide
    # with the cube's atmosphere-mesh DEM tiles).
    fire_dem_driver = DEMDriver(cache_dir="data/raw/dem_fire")

    adapter_kwargs = dict(
        sfire_dir=sfire_dir,
        ideal_cmd=[str(bin_dir / "ideal.exe")],
        wrf_cmd  =[str(bin_dir / "wrf.exe")],
        sim_seconds=sim_seconds,
        fire_mesh_ratio=fmr,
        fire_dem_driver=fire_dem_driver,
    )
    if use_real:
        adapter_kwargs["real_cmd"] = [str(bin_dir / "real.exe")]
        adapter_kwargs["met_em_dir"] = met_em_dir

    # Wrap each fetch-shaped driver in DataDriverAdapter so the engine
    # gets the ProducerV2 contract (.run, .is_satisfied, etc.) without
    # touching the driver code.
    reg = ProducerRegistry()
    for drv in (ThermalDriver(kml_path=str(kml)),
                 LandfireFBFM13Driver(),
                 DEMDriver(),
                 ERA5WindDriver()):
        reg.register(DataDriverAdapter(driver=drv))
    reg.register(WRFSFireAdapter(**adapter_kwargs))
    return reg


def print_plan(args: argparse.Namespace, t0: datetime,
                t_end: datetime, wind_end: datetime):
    print("=" * 70)
    print("PLAN")
    print("=" * 70)
    print(f"AOI center   : ({args.lon}, {args.lat})")
    print(f"AOI radius   : {args.radius_m / 1000:.1f} km")
    print(f"Atmosphere   : {args.pixel_m:.0f} m")
    fire_m = args.pixel_m / args.fire_mesh_ratio
    print(f"Fire mesh    : {fire_m:.0f} m  (fmr={args.fire_mesh_ratio})")
    print(f"WRF run      : {args.sim_seconds} s  "
          f"({args.sim_seconds / 3600:.1f} hours)")
    print(f"Start (UTC)  : {t0.isoformat()}")
    print(f"WRF end      : {t_end.isoformat()}")
    print(f"Wind window  : {t0.isoformat()} .. {wind_end.isoformat()} "
          f"({args.wind_days} days of ERA5)")
    print(f"Cube root    : {args.out}")
    print(f"Install root : {args.install_root}")
    print(f"Mode         : "
          f"{'real.exe' if args.real else 'ideal.exe'}")
    if args.real:
        print(f"met_em_dir   : {args.met_em_dir}")
    print()


def run_bootstrap_step(args: argparse.Namespace) -> None:
    # The adapter is the public entry point for everything WRF-SFIRE
    # — including its dependency provisioning. Drives the same bootstrap
    # logic via the classmethod so the script only imports one module.
    from models.wrf_sfire_adapter import WRFSFireAdapter
    import os
    # Build the netcdf_env dict from CLI flags + current shell. The
    # subprocess in bootstrap runs in its own env; without these, WRF's
    # `./configure` will print "No environment variable NETCDF set" and
    # exit 5.
    netcdf_env: dict[str, str] = {}
    if args.netcdf:
        netcdf_env["NETCDF"] = os.path.expanduser(args.netcdf)
    if args.hdf5:
        # Accepted for CLI compatibility; the bootstrap drops HDF5/HD5
        # on its default NETCDF_classic path.
        netcdf_env["HDF5"] = os.path.expanduser(args.hdf5)
    # WRF needs this for >2 GB NetCDF files.
    netcdf_env.setdefault("WRFIO_NCD_LARGE_FILE_SUPPORT", "1")

    plan = WRFSFireAdapter.bootstrap(
        install_root=args.install_root,
        dry_run=args.dry_run,
        skip_wrf=args.skip_wrf,
        skip_wps=args.skip_wps,
        skip_wps_geog=args.skip_wps_geog,
        netcdf_env=netcdf_env or None,
    )
    print("BOOTSTRAP STEPS")
    print("-" * 70)
    print(plan)
    print()


# --------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    t0 = parse_dt(args.start)
    t_end_wrf = t0 + timedelta(seconds=args.sim_seconds)
    t_end_wind = t0 + timedelta(days=args.wind_days)

    # Wire up logging FIRST so every downstream import that calls
    # get_logger has somewhere to send records. The console threshold
    # honours --log-level; the file always captures DEBUG.
    from engine.log import configure as configure_logging
    label = f"dallas_{args.start.replace(':', '').replace('-', '')}"
    ctx = configure_logging(
        level=args.log_level, log_dir=args.log_dir, label=label)

    print_plan(args, t0, t_end_wrf, t_end_wind)
    print(f"Logs         : {ctx.log_file}")
    print(f"Run id       : {ctx.run_id}")

    if args.bootstrap:
        run_bootstrap_step(args)

    if not args.kml.exists():
        print(f"ERROR: KML not found at {args.kml}", file=sys.stderr)
        return 2

    cube = build_cube(args.out, args.lon, args.lat,
                       args.radius_m, args.pixel_m)
    try:
        H, W = cube.grid.shape
        print(f"Cube grid    : {H} x {W} cells "
              f"({H * args.pixel_m / 1000:.1f} km x "
              f"{W * args.pixel_m / 1000:.1f} km)")
        Hf = H * args.fire_mesh_ratio
        Wf = W * args.fire_mesh_ratio
        print(f"Fire mesh    : {Hf} x {Wf} cells")
        print()

        reg = build_registry(
            kml=args.kml,
            install_root=args.install_root,
            sim_seconds=args.sim_seconds,
            fmr=args.fire_mesh_ratio,
            use_real=args.real,
            met_em_dir=args.met_em_dir,
        )

        print("REGISTRY")
        print("-" * 70)
        for var, prod in sorted(reg.variables().items()):
            print(f"  {var:<22} <- {prod}")
        print()

        if args.dry_run:
            print("--dry-run set; not running PipelineRunner.")
            return 0

        # ------------------------------------------------------------------
        # Wind first (covers the full 10-day window the user wants in cube),
        # then the model targets — keeps the orchestration explicit when the
        # wind window > sim_seconds.
        # ------------------------------------------------------------------
        from engine import Pipeline, PipelineRunner

        # `result_dir` -> the runner drops `runresult_*.json` per .run()
        # under args.log_dir. Disable per-call via result_path=False.
        runner = PipelineRunner(reg, verbose=True, result_dir=args.log_dir)

        print("STEP 1/2  resolve wind over the full 10-day window")
        runner.run(
            cube,
            Pipeline.from_targets(
                ["wind_speed_ms", "wind_dir_deg"], registry=reg),
            t_start=t0, t_end=t_end_wind,
            result_path=args.log_dir / f"{ctx.run_id}_step1_wind.json")

        print("\nSTEP 2/2  run WRF-SFIRE for the first sim_seconds window")
        result = runner.run(
            cube,
            Pipeline.from_targets(
                ["arrival_s", "fire_area"], registry=reg),
            t_start=t0, t_end=t_end_wrf,
            result_path=args.log_dir / f"{ctx.run_id}_step2_wrf.json")
        if not result.ok:
            for step in result.steps:
                if step.status == "error":
                    print(f"FAIL {step.name}: {step.error}",
                          file=sys.stderr)
            return 1

        print()
        print("DONE.")
        print(f"  arrival_s        cube var (downsampled fire mesh -> 900 m)")
        print(f"  fire_area        cube var")
        print(f"  cube root        {args.out}")
        print(f"  catalog          {args.out / 'catalog.duckdb'}")
        return 0
    finally:
        cube.close()


if __name__ == "__main__":
    sys.exit(main())
