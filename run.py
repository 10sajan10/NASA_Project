"""End-to-end driver: build the simulation grid, fetch all data layers,
run the fire-spread model, and emit final products.

Default workflow (resolver engine, ARCO-ERA5 cloud weather, Landsat history):

    python run.py --city "Dallas TX USA" --scenario-date 2036-09-15 --days 30

The simulation grid is centred on the chosen city's lon/lat (read from the
KML's Sample-City-Locations folder). The grid radius defaults to a
fire-spread-aware auto value of `max(200 km, n_days × 8 km/day)` so the
domain is sized to whatever the run actually needs; pass --radius-km to
override. With --auto-expand the run is repeated with a larger radius if
the fire reaches the grid edge.
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
from run_logging import start_run_epoch_log


KML_NS = "{http://www.opengis.net/kml/2.2}"


def _city_lonlat(kml_path: str, city: str) -> tuple[float, float]:
    tree = etree.parse(kml_path)
    for pm in tree.findall(f".//{KML_NS}Placemark"):
        n = pm.find(f"{KML_NS}name")
        if n is not None and n.text == city:
            coords = pm.find(f".//{KML_NS}coordinates")
            if coords is None:
                continue
            tok = coords.text.strip().split(",")
            return float(tok[0]), float(tok[1])
    raise ValueError(f"city {city!r} not found in {kml_path}")


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

    show(axes[0, 0], cube.read_static("thermal_fluence"),
         "Thermal fluence (MJ/m^2)",
         norm=mc.LogNorm(vmin=0.05, vmax=1.5), cmap="inferno")
    show(axes[0, 1], cube.read_static("fbfm40"),
         "FBFM40 fuel code", cmap="terrain")
    show(axes[0, 2], cube.read_static("dem"), "DEM (m)", cmap="gist_earth")
    show(axes[1, 0], cube.read_static("ndvi"), "NDVI",
         cmap="YlGn", vmin=-0.2, vmax=0.9)
    R = cube.read_static("R_head")
    show(axes[1, 1], R, "Rothermel R_head (m/min)",
         norm=mc.LogNorm(vmin=0.01, vmax=max(0.1, float(R.max()))),
         cmap="magma")
    arr_s = cube.read_static("arrival_s")
    arr_h = np.where(arr_s >= 0, arr_s / 3600.0, np.nan)
    show(axes[1, 2], arr_h, "Arrival time (h)",
         cmap="rainbow", vmin=0, vmax=float(np.nanpercentile(arr_h, 99)))

    plt.tight_layout()
    plt.savefig(out_dir / "overview.png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"      wrote {out_dir/'overview.png'}")


def _fire_reached_edge(cube: Cube, margin_cells: int = 4) -> bool:
    """True if any cell within `margin_cells` of the grid edge has burned."""
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
                    help="grid half-width around city centroid; "
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

    ap.add_argument("--landfire",
                    default="LANDFIRE/LF2024_FBFM40_CONUS/Tif/LF2024_FBFM40_CONUS.tif")

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
    run_logger.log(
        "arguments: "
        f"city={args.city!r}, scenario_date={args.scenario_date}, "
        f"days={args.days}, engine={args.engine}, satellite={args.satellite}, "
        f"weather={args.weather}, root={args.root!r}")

    if args.from_snapshot:
        with run_logger.stage("restore cube snapshot"):
            restored = restore_snapshot(
                args.from_snapshot, args.root,
                snapshot_dir=args.snapshot_dir,
                overwrite_root=args.overwrite_root)
            print(f"[snapshot] restored {args.from_snapshot!r} -> {restored}")

    if args.recompute_fire:
        with run_logger.stage("drop fire outputs"):
            removed = drop_cube_variables(args.root, FIRE_OUTPUT_VARIABLES)
            if removed:
                print(f"[snapshot] dropped fire outputs: {', '.join(removed)}")
            else:
                print("[snapshot] no fire outputs found to drop")

    with run_logger.stage("parse scenario and city"):
        scenario_date = datetime.fromisoformat(args.scenario_date)
        lon, lat = _city_lonlat(args.kml, args.city)

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

        with run_logger.stage(f"iteration {iteration}: run {args.engine} engine"):
            if args.engine == "resolver":
                resolver = _build_resolver(args, scenario_date)
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
