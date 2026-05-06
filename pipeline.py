"""High-level pipeline: build cube, run model/data producers.

Layer 0 (static):     thermal, landfire (FBFM40), DEM (+ slope/aspect)
Layer 1 (vegetation): sentinel (NDVI/NDWI) -> LFMC
Layer 2 (weather):    cmip6 -> dead-fuel moistures (Nelson EMC) + KBDI drought
Layer 3 (fire):       Rothermel R + anisotropic Dijkstra arrival times

Each layer reads from / writes to the cube. Models call the on-the-fly fusion
helper, so a missing variable triggers the right driver automatically.
"""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cube.grid import SimulationGrid
from cube.store import Cube
from drivers.base import register, _REGISTRY
from drivers.thermal import ThermalDriver
from drivers.landfire import LandfireDriver
from drivers.dem import DEMDriver
from drivers.sentinel import SentinelDriver
from drivers.cmip6 import CMIP6Driver
from drivers.synthetic_weather import SyntheticWeatherDriver
from drivers.landsat import LandsatHistoryDriver
from drivers.era5 import ERA5HistoryDriver
from drivers.population import PopulationRasterDriver

from models import lfmc_model, dead_fuel_model, drought_model, fire_spread
from models.satellite_indices import SatelliteIndexTrendProducer
from models.era5_weather_model import ERA5WeatherPredictor
from models.population_exposure import PopulationExposureProducer

from fusion.producers import (
    DriverProducer,
    FunctionProducer,
    ProducerRegistry,
    VariableRequest,
)
from fusion.resolver import DependencyResolver


def make_grid(lon: float, lat: float, radius_m: float,
              pixel_m: float) -> SimulationGrid:
    return SimulationGrid.from_center_radius(lon, lat, radius_m, pixel_m)


def setup_drivers(*,
                  kml_path: str | Path,
                  city: str,
                  band: str,
                  pulse_seconds: float,
                  landfire_tif: str | Path,
                  scenario_date: datetime,
                  sentinel_max_cloud: float,
                  sentinel_max_scenes: int,
                  cmip_model: str,
                  cmip_scenario: str,
                  wind_dir_deg: float,
                  weather_source: str = "cmip6",
                  synthetic_kwargs: Optional[dict] = None) -> None:
    register(ThermalDriver(kml_path, city=city, band=band,
                           pulse_seconds=pulse_seconds))
    register(LandfireDriver(landfire_tif))
    register(DEMDriver())
    register(SentinelDriver(scenario_date=scenario_date,
                             max_cloud_pct=sentinel_max_cloud,
                             max_scenes=sentinel_max_scenes))
    if weather_source == "cmip6":
        register(CMIP6Driver(model=cmip_model, scenario=cmip_scenario,
                              wind_dir_deg=wind_dir_deg))
    elif weather_source == "synthetic":
        register(SyntheticWeatherDriver(**(synthetic_kwargs or {}),
                                         wind_dir_deg=wind_dir_deg))
    else:
        raise ValueError(f"layered engine does not support {weather_source!r}")


def setup_resolver(*,
                   kml_path: str | Path,
                   city: str,
                   band: str,
                   pulse_seconds: float,
                   landfire_tif: str | Path,
                   scenario_date: datetime,
                   satellite_source: str,
                   sentinel_max_cloud: float,
                   sentinel_max_scenes: int,
                   landsat_years_back: int,
                   landsat_day_window: int,
                   landsat_max_cloud: float,
                   landsat_max_scenes_per_year: int,
                   cmip_model: str,
                   cmip_scenario: str,
                   wind_dir_deg: float,
                   weather_source: str = "cmip6",
                   synthetic_kwargs: Optional[dict] = None,
                   era5_source: str | Path | None = None,
                   era5_years_back: int = 20,
                   era5_day_window: int = 21,
                   population_raster: str | Path | None = None
                   ) -> DependencyResolver:
    """Build the producer graph used by the resolver engine."""
    reg = ProducerRegistry()

    reg.register(DriverProducer(
        ThermalDriver(kml_path, city=city, band=band,
                      pulse_seconds=pulse_seconds)))
    reg.register(DriverProducer(LandfireDriver(landfire_tif)))
    reg.register(DriverProducer(DEMDriver()))

    if satellite_source == "landsat":
        reg.register(DriverProducer(LandsatHistoryDriver(
            target_date=scenario_date,
            years_back=landsat_years_back,
            day_window=landsat_day_window,
            max_cloud_pct=landsat_max_cloud,
            max_scenes_per_year=landsat_max_scenes_per_year)))
        reg.register(SatelliteIndexTrendProducer(scenario_date))
    else:
        reg.register(DriverProducer(SentinelDriver(
            scenario_date=scenario_date,
            max_cloud_pct=sentinel_max_cloud,
            max_scenes=sentinel_max_scenes)))

    if weather_source == "cmip6":
        reg.register(DriverProducer(
            CMIP6Driver(model=cmip_model, scenario=cmip_scenario,
                        wind_dir_deg=wind_dir_deg),
            time_end_mode="inclusive_day"))
    elif weather_source == "synthetic":
        reg.register(DriverProducer(
            SyntheticWeatherDriver(**(synthetic_kwargs or {}),
                                   wind_dir_deg=wind_dir_deg),
            time_end_mode="inclusive_day"))
    elif weather_source == "era5":
        reg.register(DriverProducer(ERA5HistoryDriver(
            source_path=era5_source, years_back=era5_years_back,
            day_window=era5_day_window)))
        reg.register(ERA5WeatherPredictor(day_window=7))
    else:
        raise ValueError(weather_source)

    reg.register(FunctionProducer(
        name="lfmc_model",
        produces=["lfmc_pct"],
        requires=["ndwi"],
        func=lambda cube, req: lfmc_model.run(cube)))
    reg.register(FunctionProducer(
        name="dead_fuel_model",
        produces=["dfm_1hr", "dfm_10hr", "dfm_100hr"],
        requires=["rh", "temp_c"],
        func=lambda cube, req: dead_fuel_model.run(
            cube, _require_start(req, "dead_fuel_model"), req.n_days)))
    reg.register(FunctionProducer(
        name="drought_model",
        produces=["kbdi"],
        requires=["precip_mm", "temp_c"],
        func=lambda cube, req: drought_model.run(
            cube, _require_start(req, "drought_model"), req.n_days)))
    reg.register(FunctionProducer(
        name="fire_spread",
        produces=["R_head", "LB", "arrival_s", "fire"],
        requires=[
            "fbfm40", "dem", "slope_deg", "aspect_deg",
            "burnable", "ignition_t0", "lfmc_pct",
            "wind_speed_ms", "wind_dir_deg", "rh", "temp_c",
            "dfm_1hr", "dfm_10hr", "dfm_100hr", "kbdi",
        ],
        func=lambda cube, req: list(fire_spread.run(
            cube, _require_start(req, "fire_spread"), req.n_days).keys())))

    if population_raster is not None:
        reg.register(DriverProducer(PopulationRasterDriver(population_raster)))
        reg.register(PopulationExposureProducer())

    return DependencyResolver(reg)


def _require_start(request: VariableRequest, name: str) -> datetime:
    if request.t_start is None:
        raise ValueError(f"{name} requires t_start")
    return request.t_start


def run_layer0(cube: Cube) -> None:
    print("[L0] thermal driver")
    _REGISTRY["thermal"].fetch(cube)
    print("[L0] landfire driver")
    _REGISTRY["landfire"].fetch(cube)
    print("[L0] dem driver")
    _REGISTRY["dem"].fetch(cube)


def run_layer1(cube: Cube) -> None:
    print("[L1] sentinel-2 driver -> NDVI / NDWI")
    _REGISTRY["sentinel2"].fetch(cube)
    print("[L1] LFMC (Yebra)")
    lfmc_model.run(cube)


def run_layer2(cube: Cube, day0: datetime, n_days: int,
               weather_source: str) -> None:
    name = "cmip6" if weather_source == "cmip6" else "synthetic_weather"
    print(f"[L2] {name} climate driver: {day0.date()} +{n_days} d")
    _REGISTRY[name].fetch(cube,
                          t_start=day0,
                          t_end=day0 + timedelta(days=n_days - 1))
    print(f"[L2] dead-fuel moistures (Nelson EMC)")
    dead_fuel_model.run(cube, day0, n_days)
    print(f"[L2] KBDI drought integration")
    drought_model.run(cube, day0, n_days)


def run_layer3(cube: Cube, day0: datetime, n_days: int) -> None:
    print(f"[L3] fire spread: Rothermel + Dijkstra ({n_days} d)")
    fire_spread.run(cube, day0, n_days)


def run_full(cube: Cube, day0: datetime, n_days: int,
             weather_source: str) -> None:
    run_layer0(cube)
    run_layer1(cube)
    run_layer2(cube, day0, n_days, weather_source=weather_source)
    run_layer3(cube, day0, n_days)
    cube.export_catalog()


def run_full_resolved(cube: Cube, day0: datetime, n_days: int,
                      resolver: DependencyResolver, *,
                      include_population: bool = False,
                      print_plan: bool = True) -> None:
    t_end = day0 + timedelta(days=n_days)
    targets = ["fire", "arrival_s", "R_head", "LB"]
    if include_population:
        targets.append("population_affected")
    if print_plan:
        print("[resolver] execution plan")
        print(resolver.explain_plan(cube, targets, t_start=day0, t_end=t_end))
    resolver.ensure(cube, targets, t_start=day0, t_end=t_end)
    cube.export_catalog()
