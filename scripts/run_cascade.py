#!/usr/bin/env python3
"""Generic cascade runner — model-agnostic DAG over the asteroid-impact
environmental system.

This script is NOT specific to WRF-SFIRE. It:

  1. Builds a cube on a grid derived from the scenario (KML / centre /
     radius / resolution).
  2. Builds a producer registry from the :class:`CascadeCatalog`
     (data drivers + models — WRF-SFIRE is model 1 today).
  3. Resolves the requested **target variables** into a DAG with
     ``Pipeline.from_targets`` — which walks requires/produces
     backwards, so a model that needs another model's output pulls it
     in automatically.
  4. Executes the DAG on whatever HPC the run lands on (auto-detected
     profile; optional SLURM submission).
  5. Leaves every output in the cube at its native resolution, with
     lineage recorded.

Because targets drive the graph, the same script runs:

    # just the wildfire model (model 1)
    run_cascade.py --targets arrival_s,fire_area

    # a future downstream model (model 2) that consumes arrival_s —
    # WRF-SFIRE runs automatically in front of it
    run_cascade.py --targets flood_depth

Examples
--------
Inspect the plan without touching the network or running anything::

    python scripts/run_cascade.py --dry-run

Run the wildfire model for the Dallas scenario on the local node::

    python scripts/run_cascade.py \\
        --kml Dallas.kml --center -96.809 32.780 --radius-km 50 \\
        --pixel-m 900 --fire-mesh-ratio 10 \\
        --start 2019-09-04T12:00 --sim-hours 24 \\
        --targets arrival_s,fire_area

Use real.exe with WPS-generated met_em (auto-runs WPS first)::

    python scripts/run_cascade.py --real --build-met-em \\
        --domain-km 600 --dx-m 3000
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------- CLI
def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    # config file — the simple way to set everything
    p.add_argument("--config", type=Path, default=None,
                   help="Scenario YAML (configs/wildfire_scenario.yaml). "
                        "Sets domain extent, nests, duration, smoke, etc. "
                        "CLI flags override individual fields.")
    p.add_argument("--bootstrap", action="store_true",
                   help="Clone + compile WRF-SFIRE + WPS (MPI build for the "
                        "detected HPC) and fetch WPS_GEOG before running. "
                        "Idempotent.")
    p.add_argument("--enable-chem", action="store_true",
                   help="Bootstrap a WRF-Chem-enabled binary (needed for "
                        "smoke tracers). Implies a recompile.")

    # scenario / AOI
    p.add_argument("--kml", type=Path, default=PROJECT_ROOT / "Dallas.kml",
                   help="PDC thermal-damage KML defining the impact zone.")
    p.add_argument("--city", default="Dallas TX USA",
                   help="City name inside the KML.")
    p.add_argument("--band", default="Mean",
                   help="Thermal band (Mean/Min/Max).")
    p.add_argument("--center", type=float, nargs=2,
                   metavar=("LON", "LAT"), default=[-96.80889, 32.77998],
                   help="AOI centre lon lat.")
    p.add_argument("--radius-km", type=float, default=50.0,
                   help="AOI radius (km).")

    # grid
    p.add_argument("--pixel-m", type=float, default=900.0,
                   help="Atmosphere-mesh resolution (m).")
    p.add_argument("--fire-mesh-ratio", type=int, default=10,
                   help="Fire mesh refinement (fire px = pixel_m / ratio).")

    # time
    p.add_argument("--start", default="2019-09-04T12:00",
                   help="Scenario start (UTC ISO 8601).")
    p.add_argument("--sim-hours", type=float, default=24.0,
                   help="WRF-SFIRE simulation length (hours).")
    p.add_argument("--wind-days", type=int, default=10,
                   help="Days of ERA5 wind to cache in the cube.")

    # targets — what to produce. Drives the whole DAG.
    p.add_argument("--allow-unplaceable", action="store_true",
                   help="Launch even when the publication preflight shows a "
                        "target cannot be placed onto the cube grid.")
    p.add_argument("--targets", default="arrival_s,fire_area",
                   help="Comma-separated cube variables to produce. "
                        "The DAG is built backwards from these.")

    # execution / HPC
    p.add_argument("--hpc-profile", default=None,
                   help="Force an HPC profile (chpc_utah, stampede3, "
                        "derecho, frontier, generic_slurm). Auto-detected "
                        "if omitted.")
    p.add_argument("--np", type=int, default=0,
                   help="MPI ranks for wrf.exe. 0 -> profile default.")
    p.add_argument("--backend", default="serial",
                   choices=("serial", "thread", "process"),
                   help="Engine execution backend for the producer DAG.")
    p.add_argument("--submit-slurm", action="store_true",
                   help="Submit the WRF run as a SLURM batch job instead "
                        "of running it in this process.")

    # WRF run mode + WPS
    p.add_argument("--real", action="store_true",
                   help="Use real.exe (needs met_em). Default: ideal.exe.")
    p.add_argument("--build-met-em", action="store_true",
                   help="Run the WPS chain (ERA5->geogrid->ungrib->metgrid) "
                        "to generate met_em before the model. Implies --real.")
    p.add_argument("--met-em-dir", type=Path, default=None,
                   help="Pre-built met_em directory (skips WPS).")
    p.add_argument("--domain-km", type=float, default=600.0,
                   help="WPS parent domain side length (km) for met_em.")
    p.add_argument("--dx-m", type=float, default=3000.0,
                   help="WPS grid spacing (m) for met_em.")

    # install layout
    p.add_argument("--install-root", type=Path,
                   default=PROJECT_ROOT.parent / "WRF",
                   help="Root containing WRF-SFIRE/ and WPS/ (compiled). "
                        "Default: ../WRF (the existing compiled stack).")
    p.add_argument("--templates-dir", type=Path,
                   default=PROJECT_ROOT / "templates",
                   help="Directory with namelist templates + Vtable.")

    # cube + logging
    p.add_argument("--out", type=Path,
                   default=PROJECT_ROOT / "data" / "runs" / "cascade",
                   help="Cube root (Zarr + DuckDB).")
    p.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "logs",
                   help="Log + RunResult JSON directory.")
    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--dry-run", action="store_true",
                   help="Print the resolved plan; do not run anything.")
    return p.parse_args(argv)


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    from engine.log import configure as configure_logging
    from models.catalog import (
        ScenarioConfig, default_catalog, make_context)
    from hpc.profiles import detect_profile

    # ---- optional: load everything from a scenario YAML ----
    wrf_scenario = None
    loaded = None
    if args.config is not None:
        from models.wrf_config import load_scenario_yaml
        loaded = load_scenario_yaml(args.config)
        wrf_scenario = loaded.wrf
        _apply_loaded_scenario(args, loaded)

    t0 = datetime.fromisoformat(args.start)
    sim_seconds = int(args.sim_hours * 3600)
    if wrf_scenario is not None:
        t0 = wrf_scenario.start
        sim_seconds = int(wrf_scenario.duration_days * 86400)
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    use_real = bool(args.real or args.build_met_em)

    label = f"cascade_{t0.isoformat().replace(':', '').replace('-', '')}"
    ctx_log = configure_logging(
        level=args.log_level, log_dir=args.log_dir, label=label)

    profile = detect_profile(args.hpc_profile)

    # ---- scenario config (cube/AOI) ----
    config = ScenarioConfig(
        kml=args.kml, city=args.city, band=args.band,
        center_lon=args.center[0], center_lat=args.center[1],
        radius_m=args.radius_km * 1000.0,
        pixel_m=args.pixel_m, fire_mesh_ratio=args.fire_mesh_ratio,
        start=t0, sim_seconds=sim_seconds, wind_days=args.wind_days,
        use_real=use_real)

    # ---- banner ----
    _print_banner(args, config, profile, targets, ctx_log)
    if wrf_scenario is not None:
        _print_nest_banner(wrf_scenario)

    if not args.kml.exists():
        print(f"ERROR: KML not found: {args.kml}", file=sys.stderr)
        return 2

    # ---- optional: bootstrap the compiled stack (MPI / chem) ----
    if args.bootstrap:
        _bootstrap(args, profile)

    # ---- optional: build met_em via WPS ----
    met_em_dir = args.met_em_dir
    if args.build_met_em and not args.dry_run:
        met_em_dir = _build_met_em(args, config, profile, wrf_scenario)

    # ---- build context + registry from the catalog ----
    ctx = make_context(
        config,
        install_root=args.install_root,
        templates_dir=args.templates_dir,
        hpc_profile=args.hpc_profile,
        np=args.np,
        met_em_dir=met_em_dir,
        work_dir=args.out / "work",
        wrf_scenario=wrf_scenario,
        targets=tuple(targets))
    catalog = default_catalog()
    reg = catalog.build_registry(ctx)

    # ---- resolve the DAG from targets ----
    from engine import Pipeline
    pipeline = Pipeline.from_targets(targets, registry=reg)

    print("\nRESOLVED DAG")
    print("-" * 70)
    print(pipeline.explain())
    print("\nVARIABLE -> PRODUCER")
    print("-" * 70)
    for var, prod in sorted(reg.variables().items()):
        mark = "  <-- target" if var in targets else ""
        print(f"  {var:<22} <- {prod}{mark}")

    if args.dry_run:
        print("\n--dry-run: nothing executed.")
        return 0

    # ---- build cube ----
    cube = _build_cube(args.out, config)
    try:
        H, W = cube.grid.shape
        print(f"\nCube grid: {H} x {W} @ {config.pixel_m:.0f} m "
              f"({H * config.pixel_m / 1000:.0f} x "
              f"{W * config.pixel_m / 1000:.0f} km)")
        print(f"Fire mesh: {H * config.fire_mesh_ratio} x "
              f"{W * config.fire_mesh_ratio} @ {config.fire_pixel_m:.0f} m")

        _publication_gate(cube, config, targets,
                          strict=not args.allow_unplaceable)

        from engine import PipelineRunner, make_backend
        backend = make_backend(args.backend)
        runner = PipelineRunner(reg, backend=backend, verbose=True,
                                result_dir=args.log_dir)

        # Wind window can exceed sim_seconds: resolve time-vars first over
        # the full window, then the model targets over the sim window.
        wind_targets = [t for t in ("wind_speed_ms", "wind_dir_deg")
                        if reg.has_variable(t)]
        if wind_targets:
            print(f"\nSTEP 1  resolve wind over {args.wind_days} days")
            runner.run(
                cube,
                Pipeline.from_targets(wind_targets, registry=reg),
                t_start=t0, t_end=t0 + timedelta(days=args.wind_days),
                result_path=args.log_dir / f"{ctx_log.run_id}_wind.json")

        print(f"\nSTEP 2  run cascade targets: {', '.join(targets)}")
        result = runner.run(
            cube, pipeline,
            t_start=t0, t_end=t0 + timedelta(seconds=sim_seconds),
            result_path=args.log_dir / f"{ctx_log.run_id}_targets.json")

        if not result.ok:
            for step in result.steps:
                if step.status == "error":
                    print(f"FAIL {step.name}: {step.error}", file=sys.stderr)
            return 1

        print("\nDONE.")
        for t in targets:
            print(f"  {t:<20} -> cube")
        print(f"  cube root   {args.out}")
        print(f"  catalog     {args.out / 'catalog.duckdb'}")
        return 0
    finally:
        cube.close()


# ---------------------------------------------------------------- helpers
def _publication_gate(cube, config, targets, *, strict: bool) -> None:
    """Refuse a run whose outputs provably cannot be published.

    A cascade's most expensive failure is a publication error: the recorded
    `arrival_s: array shape (253, 253) != grid (1001, 1001)` cost 48.6 hours
    before it surfaced, because the only check ran at write time. Placement is
    a property of two grid descriptors, so it is decidable here, at launch,
    before any core-hour is spent.

    This asks only what the declared metadata can answer, and it is deliberately
    model-agnostic: any producer that declares a native grid is checked the same
    way. Producers that declare nothing are reported, not guessed at.
    """
    from cube.preflight import PlannedPublication, preflight_publications

    declared = getattr(config, "producer_native_grids", None) or {}
    # A producer that declares a native grid emits an array on *that* grid,
    # not on the cube's.  Assuming the cube's shape here would be the same
    # unfounded assumption that produced the recorded failure.
    planned = []
    for name in targets:
        native = declared.get(name)
        shape = tuple(native.shape) if native is not None \
            else tuple(cube.grid.shape)
        planned.append(PlannedPublication(name, shape, native))
    result = preflight_publications(cube.grid, planned)
    undeclared = [name for name in targets if name not in declared]

    print("\nPublication preflight:")
    if undeclared:
        print(f"  {len(undeclared)} of {len(targets)} targets declare no native "
              f"grid, so placement cannot be established for them: "
              f"{', '.join(undeclared)}")
    if result.ok:
        print("  every declared publication places onto the cube grid")
        return
    print("  " + result.report().replace("\n", "\n  "))
    if strict:
        raise SystemExit(
            "refusing to launch: the run cannot publish its targets. "
            "Re-run with --allow-unplaceable to proceed anyway.")
    print("  continuing anyway (--allow-unplaceable); publication may fail "
          "at write time after the science has run")


def _build_cube(out: Path, config):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    out.mkdir(parents=True, exist_ok=True)
    grid = SimulationGrid.from_center_radius(
        config.center_lon, config.center_lat,
        config.radius_m, config.pixel_m)
    return Cube(out, grid)


def _build_met_em(args, config, profile, wrf_scenario=None):
    """The in-process WPS path (WPSMeteoDriver) has been removed.

    Build met_em out-of-band with ``scripts/run_wps_chain.sh`` (geogrid |
    ungrib | metgrid) and point the run at it with ``--met-em-dir``.
    """
    raise NotImplementedError(
        "--build-met-em is no longer supported (wps_meteo.py removed). "
        "Run WPS separately with scripts/run_wps_chain.sh, then pass "
        "--met-em-dir <WPS dir with met_em.d0*> to use real.exe.")


def _apply_loaded_scenario(args, loaded) -> None:
    """Let a --config YAML drive the run: copy its fields onto args so the
    rest of main() is config-source-agnostic. Explicit values in the YAML
    win; this keeps a single source of truth when --config is given."""
    args.kml = loaded.kml if loaded.kml.is_absolute() \
        else (PROJECT_ROOT / loaded.kml)
    args.city = loaded.city
    args.band = loaded.band
    args.center = [loaded.center_lon, loaded.center_lat]
    args.radius_km = loaded.radius_m / 1000.0
    args.pixel_m = loaded.pixel_m
    args.fire_mesh_ratio = loaded.wrf.fire_mesh_ratio
    args.start = loaded.wrf.start.isoformat()
    args.sim_hours = loaded.wrf.duration_days * 24.0
    args.targets = ",".join(loaded.targets)
    args.real = loaded.use_real
    args.build_met_em = loaded.build_met_em
    args.backend = loaded.backend
    args.np = loaded.np
    if loaded.hpc_profile:
        args.hpc_profile = loaded.hpc_profile
    if loaded.enable_chem:
        args.enable_chem = True


def _bootstrap(args, profile) -> None:
    """Clone + compile the WRF-SFIRE/WPS stack (MPI build for this HPC,
    optionally WRF-Chem) and fetch WPS_GEOG. Idempotent."""
    from models.wrf_sfire_adapter import WRFSFireAdapter
    print("\nBOOTSTRAP")
    print("-" * 70)
    plan = WRFSFireAdapter.bootstrap(
        install_root=args.install_root,
        hpc_profile=profile,
        enable_chem=args.enable_chem,
        dry_run=args.dry_run)
    print(plan)


def _print_nest_banner(sc) -> None:
    print("NESTED DOMAINS (from --config)")
    print("-" * 70)
    for i, d in enumerate(sc.domains, 1):
        tag = "  <-- fire mesh" if d.sr > 0 else ""
        print(f"  d0{i}: dx={d.dx_m:6.0f} m  {d.nx:4d}x{d.ny:<4d} "
              f"({d.nx*d.dx_m/1000:5.0f} km)  ratio={d.parent_grid_ratio}"
              f"  start=({d.i_parent_start},{d.j_parent_start})"
              f"  sr={d.sr}{tag}")
    if sc.smoke:
        print(f"  smoke/chem: chem_opt={sc.chem_opt} (WRF-Chem binary required)")
    print()


def _print_banner(args, config, profile, targets, ctx_log):
    print("=" * 70)
    print("ASTEROID-IMPACT CASCADE")
    print("=" * 70)
    print(f"HPC profile  : {profile.name} "
          f"(launcher={profile.mpi_launcher}, default_np={profile.default_np})")
    print(f"AOI          : ({config.center_lon}, {config.center_lat}) "
          f"r={config.radius_m/1000:.0f} km")
    print(f"Resolution   : atm {config.pixel_m:.0f} m / "
          f"fire {config.fire_pixel_m:.0f} m")
    print(f"Start (UTC)  : {config.start.isoformat()}")
    print(f"Sim length   : {config.sim_seconds/3600:.1f} h")
    print(f"Targets      : {', '.join(targets)}")
    print(f"WRF mode     : {'real.exe' if config.use_real else 'ideal.exe'}")
    print(f"Install root : {args.install_root}")
    print(f"Cube root    : {args.out}")
    print(f"Run id       : {ctx_log.run_id}")
    print(f"Logs         : {ctx_log.log_file}")


if __name__ == "__main__":
    sys.exit(main())
