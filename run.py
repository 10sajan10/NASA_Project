"""End-to-end driver: build the simulation grid, fetch all data layers,
run the fire-spread model, and emit final products.

    python run.py --city "Dallas TX USA" --scenario-date 2036-09-15 --days 30

The simulation grid is centred on the chosen city's lon/lat (read from the
KML's "Sample City Location Points") with a configurable radius. All data
sources (thermal, fuels, DEM, NDVI/NDWI, climate) project onto this grid.
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from lxml import etree

import pipeline as pipe
from cube.grid import SimulationGrid
from cube.store import Cube
from cube.raster import write_geotiff


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
    """Mirror key cube variables to GeoTIFF for QGIS / external viewing."""
    keys = ["thermal_fluence", "thermal_power", "ignition_t0", "burnable",
            "fbfm40", "dem", "slope_deg", "aspect_deg",
            "ndvi", "ndwi", "nbr", "lfmc_pct",
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

    show(axes[0, 0],
         cube.read_static("thermal_fluence"),
         "Thermal fluence (MJ/m^2)",
         norm=mc.LogNorm(vmin=0.05, vmax=1.5), cmap="inferno")
    show(axes[0, 1],
         cube.read_static("fbfm40"),
         "FBFM40 fuel code", cmap="terrain")
    show(axes[0, 2],
         cube.read_static("dem"), "DEM (m)", cmap="gist_earth")
    show(axes[1, 0],
         cube.read_static("ndvi"), "NDVI", cmap="YlGn", vmin=-0.2, vmax=0.9)
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kml", default="Dallas.kml")
    ap.add_argument("--city", default="Dallas TX USA")
    ap.add_argument("--band", default="Mean")
    ap.add_argument("--pulse-s", type=float, default=10.0)
    ap.add_argument("--scenario-date", default="2036-09-15",
                    help="ISO date; can be in the future (CMIP6 horizon)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--radius-km", type=float, default=100.0,
                    help="grid half-width around city centroid")
    ap.add_argument("--pixel-m", type=float, default=100.0)
    ap.add_argument("--root", default="data",
                    help="cube root (catalog.duckdb + cube/*.zarr)")
    ap.add_argument("--engine", choices=["resolver", "layered"],
                    default="resolver",
                    help="resolver builds model/data dependencies automatically")
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
    ap.add_argument("--cmip-model", default="ACCESS-CM2")
    ap.add_argument("--cmip-scenario", default="ssp370")
    ap.add_argument("--wind-dir-deg", type=float, default=180.0,
                    help="wind FROM direction (compass deg, CW from N)")
    ap.add_argument("--weather", choices=["cmip6", "synthetic", "era5"],
                    default="cmip6")
    ap.add_argument("--era5-source",
                    help="local ERA5 NetCDF/Zarr for resolver weather=era5")
    ap.add_argument("--era5-years-back", type=int, default=20)
    ap.add_argument("--era5-day-window", type=int, default=21)
    ap.add_argument("--syn-temp-c", type=float, default=30.0)
    ap.add_argument("--syn-rh-pct", type=float, default=30.0)
    ap.add_argument("--syn-wind-ms", type=float, default=7.0)
    ap.add_argument("--syn-precip-mm", type=float, default=0.0)
    ap.add_argument("--population-raster",
                    help="optional population-count raster for exposure overlay")
    ap.add_argument("--no-plan", action="store_true",
                    help="do not print the resolver execution plan")
    args = ap.parse_args()

    scenario_date = datetime.fromisoformat(args.scenario_date)

    lon, lat = _city_lonlat(args.kml, args.city)
    print(f"[grid] city {args.city} -> ({lon:.4f}, {lat:.4f})  "
          f"radius={args.radius_km} km  pixel={args.pixel_m} m")
    grid = pipe.make_grid(lon, lat, args.radius_km * 1000.0, args.pixel_m)
    print(f"[grid] CRS=EPSG:{grid.crs_epsg}  shape={grid.shape}")

    cube = Cube(args.root, grid)
    cube.catalog.save_scenario(name=args.city, grid=grid,
                               scenario_date=scenario_date)

    synthetic_kwargs = {"temp_c": args.syn_temp_c,
                        "rh_pct": args.syn_rh_pct,
                        "wind_ms": args.syn_wind_ms,
                        "daily_precip_mm": args.syn_precip_mm}
    if args.engine == "resolver":
        resolver = pipe.setup_resolver(
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
            era5_source=args.era5_source,
            era5_years_back=args.era5_years_back,
            era5_day_window=args.era5_day_window,
            population_raster=args.population_raster,
        )
        pipe.run_full_resolved(
            cube, day0=scenario_date, n_days=args.days,
            resolver=resolver,
            include_population=args.population_raster is not None,
            print_plan=not args.no_plan)
    else:
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

    print("[output] writing GeoTIFFs and overview")
    out = Path(args.root) / "out"
    out.mkdir(parents=True, exist_ok=True)
    _emit_geotiffs(cube, out)
    _emit_overview(cube, out)
    cube.close()
    print("[done]")


if __name__ == "__main__":
    main()
