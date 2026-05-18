"""End-to-end driver: build the simulation grid, fetch all data layers,
run the fire-spread model, and emit final products.

Default workflow (resolver engine, ARCO-ERA5 cloud weather, Landsat history):

    python run.py --city "Dallas TX USA" --scenario-date 2036-09-15 --days 30

The simulation grid is centered on the chosen city's lon/lat when the KML has
a matching point Placemark. Simplified KMLs with only one thermal ring set are
centered on the ring centroid instead. The grid radius defaults to a
fire-spread-aware auto value of `max(200 km, n_days x 8 km/day)` so the domain
is sized to whatever the run actually needs; pass --radius-km to override.
With --auto-expand the run is repeated with a larger radius if the fire reaches
the grid edge.
"""
from __future__ import annotations

import argparse
import atexit
from datetime import datetime
from pathlib import Path

import numpy as np
from lxml import etree

import pipeline as pipe
from cube.grid import SimulationGrid
from cube.store import Cube
from cube.raster import write_geotiff
from cube.snapshot import (
    FIRE_OUTPUT_VARIABLES,
    drop_cube_variables,
    restore_snapshot,
    save_snapshot,
)
from drivers.kml import load_city_damage
from engine import RunManifest
from engine.schema import CURRENT_SCHEMA_VERSION, ensure_schema
from run_logging import start_run_epoch_log


KML_NS = "{http://www.opengis.net/kml/2.2}"


def _scenario_lonlat(kml_path: str, city: str, band: str) -> tuple[float, float]:
    tree = etree.parse(kml_path)
    for pm in tree.findall(f".//{KML_NS}Placemark"):
        n = pm.find(f"{KML_NS}name")
        if n is not None and n.text == city:
            coords = pm.find(f".//{KML_NS}coordinates")
            if coords is None:
                continue
            tok = coords.text.strip().split(",")
            return float(tok[0]), float(tok[1])

    damage = load_city_damage(kml_path, city=city, band=band)
    center = damage.rings[-1].polygon.centroid
    print("[kml] city point not found; using thermal-ring centroid "
          f"from {damage.band!r} ring set")
    return float(center.x), float(center.y)


def _emit_geotiffs(cube: Cube, out_dir: Path) -> None:
    keys = ["thermal_fluence", "thermal_power", "ignition_t0", "burnable",
            "fbfm40", "dem", "slope_deg", "aspect_deg",
            "ndvi", "ndwi", "nbr", "lfmc_pct",
            "hard_barrier", "urban_mask", "surface_spread_class",
            "ignition_threshold_mj_m2", "spread_threshold_kw_m",
            "spread_rate_modifier", "ignition_effective_t0",
            "fireline_intensity_kw_m",
            "R_head", "LB", "arrival_s",
            "population", "population_affected"]
    seen = set(keys)
    for meta in cube.catalog.list_variables():
        if meta["kind"] == "static" and meta["name"] not in seen:
            keys.append(meta["name"])
            seen.add(meta["name"])
    for k in keys:
        try:
            arr = cube.read_static(k)
        except Exception:
            continue
        nd = float("nan") if arr.dtype.kind == "f" else 255
        write_geotiff(
            out_dir / f"{k}.tif",
            arr,
            (cube.grid.pixel_m, 0, cube.grid.x0,
             0, -cube.grid.pixel_m, cube.grid.y1),
            cube.grid.crs, nodata=nd)
    print(f"      wrote GeoTIFFs to {out_dir}/")


def _emit_overview(cube: Cube, out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.colors as mc

    fig, axes = plt.subplots(2, 3, figsize=(18, 11))
    H = cube.grid.height; W = cube.grid.width
    extent = (cube.grid.x0, cube.grid.x0 + W * cube.grid.pixel_m,
              cube.grid.y1 - H * cube.grid.pixel_m, cube.grid.y1)

    def show(ax, arr, title, **kw):
        im = ax.imshow(arr, extent=extent, origin="upper", **kw)
        ax.set_title(title); plt.colorbar(im, ax=ax, fraction=0.046)

    def show_optional(ax, variable, title, **kw):
        if not cube.has(variable):
            ax.set_title(title)
            ax.text(0.5, 0.5, f"{variable} not produced",
                    ha="center", va="center", transform=ax.transAxes)
            ax.set_axis_off()
            return None
        return show(ax, cube.read_static(variable), title, **kw)

    show_optional(axes[0, 0], "thermal_fluence",
                  "Thermal fluence (MJ/m^2)",
                  norm=mc.LogNorm(vmin=0.05, vmax=1.5), cmap="inferno")
    show_optional(axes[0, 1], "fbfm40", "FBFM40 fuel code", cmap="terrain")
    show_optional(axes[0, 2], "dem", "DEM (m)", cmap="gist_earth")
    show_optional(axes[1, 0], "ndvi", "NDVI",
                  cmap="YlGn", vmin=-0.2, vmax=0.9)
    if cube.has("R_head"):
        R = cube.read_static("R_head")
        show(axes[1, 1], R, "Rothermel R_head (m/min)",
             norm=mc.LogNorm(vmin=0.01, vmax=max(0.1, float(R.max()))),
             cmap="magma")
    else:
        axes[1, 1].set_title("Spread rate")
        axes[1, 1].text(0.5, 0.5, "R_head not produced",
                        ha="center", va="center",
                        transform=axes[1, 1].transAxes)
        axes[1, 1].set_axis_off()
    if cube.has("arrival_s"):
        arr_s = cube.read_static("arrival_s")
        arr_h = np.where(arr_s >= 0, arr_s / 3600.0, np.nan)
        show(axes[1, 2], arr_h, "Arrival time (h)",
             cmap="rainbow", vmin=0, vmax=float(np.nanpercentile(arr_h, 99)))
    else:
        axes[1, 2].set_title("Arrival time (h)")
        axes[1, 2].text(0.5, 0.5, "arrival_s not produced",
                        ha="center", va="center",
                        transform=axes[1, 2].transAxes)
        axes[1, 2].set_axis_off()

    plt.tight_layout()
    plt.savefig(out_dir / "overview.png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"      wrote {out_dir/'overview.png'}")


def _fire_reached_edge(cube: Cube, margin_cells: int = 4) -> bool:
    """True if any cell within `margin_cells` of the grid edge has burned."""
    if not cube.has("arrival_s"):
        print("[auto-expand] selected fire model did not produce arrival_s; "
              "skipping edge check")
        return False
    arr = cube.read_static("arrival_s")
    m = margin_cells
    edge = np.concatenate([
        arr[:m].ravel(), arr[-m:].ravel(),
        arr[:, :m].ravel(), arr[:, -m:].ravel()])
    return bool((edge >= 0).any())


def _grid_radius_m(grid: SimulationGrid) -> float:
    return max(grid.width, grid.height) * grid.pixel_m / 2.0


def _build_resolver(args, scenario_date):
    synthetic_kwargs = {"temp_c": args.syn_temp_c,
                        "rh_pct": args.syn_rh_pct,
                        "wind_ms": args.syn_wind_ms,
                        "daily_precip_mm": args.syn_precip_mm}
    return pipe.setup_resolver(
        kml_path=args.kml, city=args.city, band=args.band,
        pulse_seconds=args.pulse_s,
        landfire_tif=args.landfire,
        landfire_fbfm13_tif=args.landfire_fbfm13,
        scenario_date=scenario_date,
        satellite_source=args.satellite,
        sentinel_max_cloud=args.sentinel_max_cloud,
        sentinel_max_scenes=args.sentinel_max_scenes,
        landsat_years_back=args.landsat_years_back,
        landsat_day_window=args.landsat_day_window,
        landsat_max_cloud=args.landsat_max_cloud,
        landsat_max_scenes_per_year=args.landsat_max_scenes_per_year,
        cmip_model=args.cmip_model, cmip_scenario=args.cmip_scenario,
        wind_dir_deg=args.wind_dir_deg,
        weather_source=args.weather,
        synthetic_kwargs=synthetic_kwargs,
        era5_years_back=args.era5_years_back,
        era5_day_window=args.era5_day_window,
        regression_n_harmonics=args.harmonics,
        population_raster=args.population_raster,
        fire_model=args.fire_model,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kml", default="Dallas.kml")
    ap.add_argument("--city", default="Dallas TX USA")
    ap.add_argument("--band", default="Mean")
    ap.add_argument("--pulse-s", type=float, default=10.0)
    ap.add_argument("--scenario-date", default="2036-09-15")
    ap.add_argument("--days", type=int, default=30)

    ap.add_argument("--radius-km", type=float, default=None,
                    help="grid half-width around scenario center; "
                         "default = max(200, days * 8) km")
    ap.add_argument("--max-radius-km", type=float, default=600.0,
                    help="upper bound on auto-expansion")
    ap.add_argument("--auto-expand", action="store_true",
                    help="if fire reaches the grid edge, grow the grid "
                         "and re-run until it doesn't (capped by --max-radius-km)")
    ap.add_argument("--pixel-m", type=float, default=100.0)
    ap.add_argument("--root", default="data")
    ap.add_argument("--log-file", default="logs/run_epoch.log",
                    help="append terminal output and stage markers here")
    ap.add_argument("--from-snapshot",
                    help="restore a named/path cube snapshot into --root before running")
    ap.add_argument("--save-snapshot",
                    help="save the finished --root cube as this snapshot name/path")
    ap.add_argument("--snapshot-dir", default="cube_snapshots",
                    help="directory for named cube snapshots")
    ap.add_argument("--overwrite-root", action="store_true",
                    help="allow --from-snapshot to replace an existing --root")
    ap.add_argument("--overwrite-snapshot", action="store_true",
                    help="allow --save-snapshot to replace an existing snapshot")
    ap.add_argument("--recompute-fire", action="store_true",
                    help="drop fire outputs before running, preserving upstream inputs")
    ap.add_argument("--engine", choices=["resolver", "layered"],
                    default="resolver")
    ap.add_argument("--orchestrator", choices=["legacy", "engine"],
                    default="engine",
                    help="engine (default) = model-agnostic PipelineRunner "
                         "over a swappable Backend (serial/thread/process/"
                         "dask/slurm) with cube.satisfies-aware skip, "
                         "merge-policy enforcement, retries, tile metrics, "
                         "and dirty propagation. legacy = deprecated fusion "
                         "DependencyResolver path, kept for emergency "
                         "fallback.")
    ap.add_argument("--engine-backend",
                    choices=["serial", "thread", "process", "dask", "slurm"],
                    default="serial",
                    help="execution backend when --orchestrator=engine")
    ap.add_argument("--engine-workers", type=int, default=None,
                    help="max workers for thread/process/dask backends")
    ap.add_argument("--fire-model", choices=pipe.available_fire_models(),
                    default="rothermel",
                    help="fire-model adapter to use; each adapter declares "
                         "the cube variables it needs")

    ap.add_argument("--landfire",
                    default="LANDFIRE/LF2024_FBFM40_CONUS/Tif/LF2024_FBFM40_CONUS.tif")
    ap.add_argument("--landfire-fbfm13",
                    default="LANDFIRE/LF2024_FBFM13_CONUS/Tif/LF2024_FBFM13_CONUS.tif",
                    help="LANDFIRE FBFM13 raster used by WRF-Fire-style "
                         "adapters that request nfuel_cat")

    ap.add_argument("--satellite", choices=["landsat", "sentinel"],
                    default="landsat")
    ap.add_argument("--sentinel-max-cloud", type=float, default=30.0)
    ap.add_argument("--sentinel-max-scenes", type=int, default=8)
    ap.add_argument("--landsat-years-back", type=int, default=12)
    ap.add_argument("--landsat-day-window", type=int, default=30)
    ap.add_argument("--landsat-max-cloud", type=float, default=50.0)
    ap.add_argument("--landsat-max-scenes-per-year", type=int, default=3)

    ap.add_argument("--weather", choices=["era5", "cmip6", "synthetic"],
                    default="era5",
                    help="era5 = ARCO-ERA5 history + per-pixel regression "
                         "(recommended); cmip6 = NEX-GDDP future projection; "
                         "synthetic = constants for testing")
    ap.add_argument("--era5-years-back", type=int, default=12)
    ap.add_argument("--era5-day-window", type=int, default=21)
    ap.add_argument("--cmip-model", default="ACCESS-CM2")
    ap.add_argument("--cmip-scenario", default="ssp370")
    ap.add_argument("--wind-dir-deg", type=float, default=180.0)
    ap.add_argument("--syn-temp-c", type=float, default=30.0)
    ap.add_argument("--syn-rh-pct", type=float, default=30.0)
    ap.add_argument("--syn-wind-ms", type=float, default=7.0)
    ap.add_argument("--syn-precip-mm", type=float, default=0.0)

    ap.add_argument("--harmonics", type=int, default=2,
                    help="seasonal harmonics in the per-pixel regression")
    ap.add_argument("--population-raster")
    ap.add_argument("--no-plan", action="store_true")
    args = ap.parse_args()

    run_logger = start_run_epoch_log(args.log_file)
    atexit.register(run_logger.close)
    if args.orchestrator == "legacy":
        run_logger.log(
            "WARNING: --orchestrator=legacy is deprecated. The engine "
            "orchestrator is the default (model-agnostic, scales, retries, "
            "lineage). Drop --orchestrator=legacy to use the recommended "
            "path. Legacy will be removed in a future release.")
    run_logger.log(
        "arguments: "
        f"city={args.city!r}, scenario_date={args.scenario_date}, "
        f"days={args.days}, engine={args.engine}, "
        f"orchestrator={args.orchestrator}, "
        f"engine_backend={args.engine_backend}, "
        f"satellite={args.satellite}, "
        f"weather={args.weather}, fire_model={args.fire_model}, "
        f"root={args.root!r}")

    if args.from_snapshot:
        with run_logger.stage("restore cube snapshot"):
            restored = restore_snapshot(
                args.from_snapshot, args.root,
                snapshot_dir=args.snapshot_dir,
                overwrite_root=args.overwrite_root)
            print(f"[snapshot] restored {args.from_snapshot!r} -> {restored}")

    if args.recompute_fire:
        with run_logger.stage("drop fire outputs"):
            fire_outputs = sorted(
                set(FIRE_OUTPUT_VARIABLES)
                | set(pipe.fire_model_outputs(args.fire_model)))
            removed = drop_cube_variables(args.root, fire_outputs)
            if removed:
                print(f"[snapshot] dropped fire outputs: {', '.join(removed)}")
            else:
                print("[snapshot] no fire outputs found to drop")

    with run_logger.stage("parse scenario geometry"):
        scenario_date = datetime.fromisoformat(args.scenario_date)
        lon, lat = _scenario_lonlat(args.kml, args.city, args.band)

        radius_m = (args.radius_km * 1000.0 if args.radius_km
                    else pipe.auto_radius_m(args.days))
        max_radius_m = args.max_radius_km * 1000.0
    iteration = 0

    while True:
        iteration += 1
        with run_logger.stage(f"iteration {iteration}: build grid"):
            if args.from_snapshot and iteration == 1:
                grid = SimulationGrid.load(Path(args.root) / "grid.json")
                radius_m = _grid_radius_m(grid)
                print(f"[grid] iter={iteration}  using restored snapshot grid  "
                      f"radius~={radius_m/1000:.0f} km  pixel={grid.pixel_m} m")
            else:
                print(f"[grid] iter={iteration}  city {args.city} "
                      f"-> ({lon:.4f}, {lat:.4f})  radius={radius_m/1000:.0f} km  "
                      f"pixel={args.pixel_m} m")
                grid = pipe.make_grid(lon, lat, radius_m, args.pixel_m)
            print(f"[grid] CRS=EPSG:{grid.crs_epsg}  shape={grid.shape}")

        # fresh root per iteration so producers re-run with new grid; previous
        # iteration's output is moved to root_iter{N}/
        if iteration > 1:
            with run_logger.stage(f"iteration {iteration}: archive previous root"):
                old = Path(args.root)
                archive = old.with_name(f"{old.name}_iter{iteration-1}")
                if old.exists():
                    old.rename(archive)

        with run_logger.stage(f"iteration {iteration}: create cube"):
            cube = Cube(args.root, grid)
            cube.catalog.save_scenario(name=args.city, grid=grid,
                                       scenario_date=scenario_date)
            ensure_schema(cube.catalog.path,
                          target=CURRENT_SCHEMA_VERSION)
            print(f"[cube] schema_version={CURRENT_SCHEMA_VERSION}")

        with run_logger.stage(f"iteration {iteration}: open run manifest"):
            manifest = RunManifest.create(
                cube.catalog.path, vars(args),
                git_root=Path(__file__).resolve().parent)
            for role, path in (("kml", args.kml),
                               ("landfire_fbfm40", args.landfire),
                               ("landfire_fbfm13", args.landfire_fbfm13),
                               ("population", args.population_raster)):
                if path:
                    manifest.record_input(role, path)
            print(f"[manifest] run_id={manifest.run_id} "
                  f"config_hash={manifest.summary().get('config_hash', '')[:12]}")

        stage_label = (f"run {args.engine} engine "
                       f"[{args.orchestrator}"
                       + (f"/{args.engine_backend}" if args.orchestrator == "engine"
                          else "") + "]")
        run_status = "ok"
        run_notes = ""
        try:
            with run_logger.stage(f"iteration {iteration}: {stage_label}"):
                if args.engine == "resolver":
                    resolver = _build_resolver(args, scenario_date)
                    if args.orchestrator == "engine":
                        backend_kwargs = ({"max_workers": args.engine_workers}
                                          if args.engine_backend
                                          in ("thread", "process")
                                          and args.engine_workers else {})
                        pipe.run_full_via_engine(
                            cube, day0=scenario_date, n_days=args.days,
                            resolver=resolver,
                            backend_mode=args.engine_backend,
                            backend_kwargs=backend_kwargs,
                            include_population=args.population_raster is not None,
                            print_plan=not args.no_plan)
                    else:
                        pipe.run_full_resolved(
                            cube, day0=scenario_date, n_days=args.days,
                            resolver=resolver,
                            include_population=args.population_raster is not None,
                            print_plan=not args.no_plan)
                else:
                    synthetic_kwargs = {"temp_c": args.syn_temp_c,
                                        "rh_pct": args.syn_rh_pct,
                                        "wind_ms": args.syn_wind_ms,
                                        "daily_precip_mm": args.syn_precip_mm}
                    pipe.setup_drivers(
                        kml_path=args.kml, city=args.city, band=args.band,
                        pulse_seconds=args.pulse_s,
                        landfire_tif=args.landfire,
                        scenario_date=scenario_date,
                        sentinel_max_cloud=args.sentinel_max_cloud,
                        sentinel_max_scenes=args.sentinel_max_scenes,
                        cmip_model=args.cmip_model, cmip_scenario=args.cmip_scenario,
                        wind_dir_deg=args.wind_dir_deg,
                        weather_source=args.weather,
                        synthetic_kwargs=synthetic_kwargs,
                    )
                    pipe.run_full(cube, day0=scenario_date, n_days=args.days,
                                  weather_source=args.weather)
        except BaseException as e:
            run_status = "error"
            run_notes = f"{type(e).__name__}: {e}"
            manifest.finalize(status=run_status, notes=run_notes)
            raise
        finally:
            if run_status == "ok":
                # Record produced variables (best-effort).
                try:
                    for meta in cube.list_variables():
                        manifest.record_output(
                            meta["name"], version=0,
                            producer=meta.get("producer", "") or "")
                except Exception:
                    pass
                manifest.finalize(status="ok")

        if args.auto_expand:
            with run_logger.stage(f"iteration {iteration}: auto-expand check"):
                reached_edge = _fire_reached_edge(cube)
            if reached_edge:
                new_r = min(radius_m * 1.5, max_radius_m)
                if new_r > radius_m + 1e-3:
                    cube.close()
                    print(f"[auto-expand] fire reached grid edge; "
                          f"expanding radius {radius_m/1000:.0f} -> "
                          f"{new_r/1000:.0f} km")
                    radius_m = new_r
                    continue
                else:
                    print(f"[auto-expand] hit --max-radius-km={args.max_radius_km}; "
                          f"stopping")

        with run_logger.stage(f"iteration {iteration}: write outputs"):
            print("[output] writing GeoTIFFs and overview")
            out = Path(args.root) / "out"
            out.mkdir(parents=True, exist_ok=True)
            _emit_geotiffs(cube, out)
            _emit_overview(cube, out)
            cube.close()

        if args.save_snapshot:
            with run_logger.stage(f"iteration {iteration}: save cube snapshot"):
                saved = save_snapshot(
                    args.root, args.save_snapshot,
                    snapshot_dir=args.snapshot_dir,
                    overwrite=args.overwrite_snapshot)
                print(f"[snapshot] saved {args.root!r} -> {saved}")
        break

    print("[done]")


if __name__ == "__main__":
    main()
