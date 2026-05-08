"""High-level pipeline: build cube, run model/data producers in dependency order.

Architecture
------------
* Data adapters fetch external data and write it to the cube (Driver subclasses).
* Model adapters consume cube variables and produce derived variables
  (BaseProducer subclasses; FunctionProducer wraps simple functions).
* DependencyResolver walks the produces/requires graph from a target variable
  back through every required producer, runs each one exactly once, and writes
  every intermediate result back to central storage.

Layering (informational; the resolver doesn't actually use these labels)
    Layer 0 (static):     thermal rings, LANDFIRE FBFM40, USGS 3DEP DEM
    Layer 1 (vegetation): Landsat history -> per-pixel multivariate
                          regression -> NDVI/NDWI/NBR -> LFMC
    Layer 2 (weather):    ARCO-ERA5 history -> per-pixel climate regression
                          -> hourly T/RH/wind/precip (RH via Magnus)
                          -> dead-fuel moistures + KBDI drought
    Layer 3 (fire):       Rothermel R + anisotropic Dijkstra arrival times
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cube.grid import SimulationGrid
from cube.store import Cube

from drivers.base import register, _REGISTRY
from drivers.thermal import ThermalDriver
from drivers.landfire import LandfireDriver, LandfireFBFM13Driver
from drivers.dem import DEMDriver
from drivers.sentinel import SentinelDriver
from drivers.cmip6 import CMIP6Driver
from drivers.synthetic_weather import SyntheticWeatherDriver
from drivers.landsat import LandsatHistoryDriver
from drivers.era5 import ARCOERA5HistoryDriver
from drivers.population import PopulationRasterDriver

from models import lfmc_model, dead_fuel_model, drought_model
from models.fire_adapters import (
    available_fire_models,
    build_fire_model_producer,
    fire_model_outputs,
)
from models.satellite_indices import SatelliteIndexRegression
from models.climate_regression import ClimateRegression
from models.fuel_thresholds import FuelThresholdProducer
from models.population_exposure import PopulationExposureProducer

from fusion.producers import (
    DriverProducer,
    FunctionProducer,
    ProducerRegistry,
    VariableRequest,
)
from fusion.resolver import DependencyResolver


# ---------------------------------------------------------------------- grid
def make_grid(lon: float, lat: float, radius_m: float,
              pixel_m: float) -> SimulationGrid:
    return SimulationGrid.from_center_radius(lon, lat, radius_m, pixel_m)


def auto_radius_m(n_days: int, *,
                  per_day_km: float = 8.0,
                  floor_km: float = 200.0) -> float:
    """Default simulation radius such that fires up to per_day_km/day stay
    inside the grid for n_days. Floor at floor_km so 1-day runs still have a
    sensible 200 km canvas."""
    radius_km = max(floor_km, n_days * per_day_km)
    return radius_km * 1000.0


# ------------------------------------------------------------------ resolver
def setup_resolver(*,
                   kml_path: str | Path,
                   city: str,
                   band: str,
                   pulse_seconds: float,
                   landfire_tif: str | Path,
                   landfire_fbfm13_tif: str | Path,
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
                   weather_source: str = "era5",
                   synthetic_kwargs: Optional[dict] = None,
                   era5_years_back: int = 12,
                   era5_day_window: int = 21,
                   regression_n_harmonics: int = 2,
                   population_raster: str | Path | None = None,
                   fire_model: str = "rothermel",
                   ) -> DependencyResolver:
    """Build the producer graph used by the resolver engine."""
    reg = ProducerRegistry()

    # ---- Layer 0 -----------------------------------------------------------
    reg.register(DriverProducer(
        ThermalDriver(kml_path, city=city, band=band,
                      pulse_seconds=pulse_seconds)))
    reg.register(DriverProducer(LandfireDriver(landfire_tif)))
    reg.register(DriverProducer(LandfireFBFM13Driver(landfire_fbfm13_tif)))
    reg.register(DriverProducer(DEMDriver()))

    # ---- Layer 1: satellite history + per-pixel regression -----------------
    if satellite_source == "landsat":
        reg.register(DriverProducer(
            LandsatHistoryDriver(
                target_date=scenario_date,
                years_back=landsat_years_back,
                day_window=landsat_day_window,
                max_cloud_pct=landsat_max_cloud,
                max_scenes_per_year=landsat_max_scenes_per_year),
            time_check_mode="any"))
        reg.register(SatelliteIndexRegression(
            scenario_date, n_harmonics=regression_n_harmonics))
    else:
        reg.register(DriverProducer(SentinelDriver(
            scenario_date=scenario_date,
            max_cloud_pct=sentinel_max_cloud,
            max_scenes=sentinel_max_scenes)))

    # ---- Layer 2: weather --------------------------------------------------
    if weather_source == "era5":
        reg.register(DriverProducer(
            ARCOERA5HistoryDriver(
                target_date=scenario_date,
                years_back=era5_years_back,
                day_window=era5_day_window),
            time_check_mode="any"))
        reg.register(ClimateRegression(n_harmonics=regression_n_harmonics))
    elif weather_source == "cmip6":
        reg.register(DriverProducer(
            CMIP6Driver(model=cmip_model, scenario=cmip_scenario,
                        wind_dir_deg=wind_dir_deg),
            time_end_mode="inclusive_day"))
    elif weather_source == "synthetic":
        reg.register(DriverProducer(
            SyntheticWeatherDriver(**(synthetic_kwargs or {}),
                                   wind_dir_deg=wind_dir_deg),
            time_end_mode="inclusive_day"))
    else:
        raise ValueError(f"unknown weather_source {weather_source!r}")

    # ---- Layer 1.5 / 2.5 derived -------------------------------------------
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
    reg.register(FuelThresholdProducer())

    # ---- Layer 3: fire spread ---------------------------------------------
    # Fire models are pluggable adapters. The selected adapter owns its
    # required/produced variable contract, so the resolver only fetches the
    # data that fire model actually needs.
    reg.register(build_fire_model_producer(fire_model))

    # ---- optional population layer ----------------------------------------
    if population_raster is not None:
        reg.register(DriverProducer(PopulationRasterDriver(population_raster)))
        reg.register(PopulationExposureProducer())

    return DependencyResolver(reg)


def _require_start(request: VariableRequest, name: str) -> datetime:
    if request.t_start is None:
        raise ValueError(f"{name} requires t_start")
    return request.t_start


# ----------------------------------------------------------- run-everything
def run_full_resolved(cube: Cube, day0: datetime, n_days: int,
                      resolver: DependencyResolver, *,
                      include_population: bool = False,
                      print_plan: bool = True) -> None:
    t_end = day0 + timedelta(days=n_days)
    targets = list(resolver.registry.get("fire_model").produces)
    if include_population:
        targets.append("population_affected")
    if print_plan:
        print("[resolver] execution plan")
        print(resolver.explain_plan(cube, targets, t_start=day0, t_end=t_end))
    resolver.ensure(cube, targets, t_start=day0, t_end=t_end)
    cube.export_catalog()


# ------------------------------------------------------- legacy layered API
# kept for the --engine layered code path; uses the same drivers but skips
# the resolver. Useful for debugging.
def setup_drivers(*, kml_path, city, band, pulse_seconds, landfire_tif,
                  scenario_date, sentinel_max_cloud, sentinel_max_scenes,
                  cmip_model, cmip_scenario, wind_dir_deg,
                  weather_source: str = "synthetic",
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
        raise ValueError(
            f"layered engine doesn't support {weather_source!r}; use resolver")


def run_layer0(cube: Cube) -> None:
    _REGISTRY["thermal"].fetch(cube)
    _REGISTRY["landfire"].fetch(cube)
    _REGISTRY["dem"].fetch(cube)


def run_layer1(cube: Cube) -> None:
    _REGISTRY["sentinel2"].fetch(cube)
    lfmc_model.run(cube)


def run_layer2(cube: Cube, day0: datetime, n_days: int,
               weather_source: str) -> None:
    name = "cmip6" if weather_source == "cmip6" else "synthetic_weather"
    _REGISTRY[name].fetch(cube, t_start=day0,
                          t_end=day0 + timedelta(days=n_days - 1))
    dead_fuel_model.run(cube, day0, n_days)
    drought_model.run(cube, day0, n_days)


def run_layer3(cube: Cube, day0: datetime, n_days: int) -> None:
    fire_spread.run(cube, day0, n_days)


def run_full(cube: Cube, day0: datetime, n_days: int,
             weather_source: str) -> None:
    run_layer0(cube)
    run_layer1(cube)
    run_layer2(cube, day0, n_days, weather_source=weather_source)
    run_layer3(cube, day0, n_days)
    cube.export_catalog()
