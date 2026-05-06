"""High-level pipeline: build cube, run all drivers + models in dependency order.

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

from models import lfmc_model, dead_fuel_model, drought_model, fire_spread


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
    else:
        register(SyntheticWeatherDriver(**(synthetic_kwargs or {}),
                                         wind_dir_deg=wind_dir_deg))


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
