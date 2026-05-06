"""Fire spread model: Rothermel R + anisotropic Dijkstra fast-marching CA.

This is a pragmatic substitute for elmfire — same physics in the inner loop
(Rothermel 1972 + Anderson 1983 elliptical anisotropy), but solved as a
Dijkstra arrival-time problem on the simulation grid instead of a level-set
PDE on the GPU.

Time-averaged weather over [t0, t0+days] is used to compute one R per cell.
That's a known simplification — the elmfire-style time-stepping with hourly
weather updates can replace `_compute_arrival` later without touching the
rest of the cube/driver/catalog plumbing.

Outputs to the cube:
  - R_head     [m/min]               static
  - LB         [-]                   static (length-to-breadth)
  - arrival_s  [s since t=0]         static (inf if never burned)
  - fire(t)    [{0,1}]               hourly time-varying
"""
from __future__ import annotations
import heapq
import math
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from cube.store import Cube
from fusion.on_the_fly import get_static
from models.fuel_models import fuel_arrays
from models.rothermel import rothermel_R


# ---- midflame wind reduction (Andrews 2012) ----
def _midflame_factor(fuel_depth_m: np.ndarray, sheltered: np.ndarray) -> np.ndarray:
    """Wind adjustment factor (10-m -> midflame).

    Unsheltered formula (Albini & Baughman 1979):
       WAF = 1.83 / ln((20 + 0.36*H) / (0.13*H))   with H in ft
    Sheltered (under canopy) cap at 0.4.
    """
    H_ft = np.maximum(fuel_depth_m / 0.3048, 0.1)
    waf = 1.83 / np.log((20.0 + 0.36 * H_ft) / (0.13 * H_ft))
    waf = np.clip(waf, 0.05, 0.6)
    waf = np.where(sheltered, np.minimum(waf, 0.4), waf)
    return waf.astype(np.float32)


def _slice_3d(cube: Cube, var: str, day0: datetime, n_days: int):
    ts, arr = cube.read_3d(var)
    t_start = day0
    t_end = day0 + timedelta(days=n_days)
    keep = [i for i, t in enumerate(ts) if t_start <= t < t_end]
    return arr[keep]


def _aggregate_weather(cube: Cube, day0: datetime, n_days: int):
    """Time-mean wind speed/dir, RH, T, and dead-fuel moistures."""
    ws = _slice_3d(cube, "wind_speed_ms", day0, n_days).astype(np.float64).mean(0)
    wd = _slice_3d(cube, "wind_dir_deg", day0, n_days).astype(np.float64)
    wdr = np.deg2rad(wd)
    wd_mean_rad = np.arctan2(np.sin(wdr).mean(0), np.cos(wdr).mean(0))
    rh  = _slice_3d(cube, "rh", day0, n_days).astype(np.float64).mean(0)
    t_c = _slice_3d(cube, "temp_c", day0, n_days).astype(np.float64).mean(0)
    dfm1   = _slice_3d(cube, "dfm_1hr",   day0, n_days).astype(np.float64).mean(0)
    dfm10  = _slice_3d(cube, "dfm_10hr",  day0, n_days).astype(np.float64).mean(0)
    dfm100 = _slice_3d(cube, "dfm_100hr", day0, n_days).astype(np.float64).mean(0)
    return (ws.astype(np.float32),
            np.rad2deg(wd_mean_rad).astype(np.float32),
            rh.astype(np.float32), t_c.astype(np.float32),
            (dfm1 / 100.0).astype(np.float32),
            (dfm10 / 100.0).astype(np.float32),
            (dfm100 / 100.0).astype(np.float32))


def _wind_to_heading_math_rad(wind_from_deg: np.ndarray) -> np.ndarray:
    """Convert compass wind-FROM direction (deg, CW from N) to math angle of
    fire-heading direction (rad, CCW from +x = east). The fire heads where
    the wind is going to:  heading_compass = (wind_from + 180) mod 360
    Math angle: 90° - heading_compass."""
    heading_compass = (wind_from_deg + 180.0) % 360.0
    return np.deg2rad((90.0 - heading_compass) % 360.0).astype(np.float32)


# ----------------------------------------------------------------------
# Anisotropic Dijkstra arrival-time solver (8-connected)
# ----------------------------------------------------------------------
def _compute_arrival(R_head_mpmin: np.ndarray,
                     LB: np.ndarray,
                     heading_rad: np.ndarray,
                     ig0: np.ndarray,
                     burnable: np.ndarray,
                     pixel_m: float) -> np.ndarray:
    H, W = R_head_mpmin.shape
    arrival = np.full((H, W), np.inf, dtype=np.float64)
    DJ = (-1, -1, -1, 0, 0, 1, 1, 1)
    DI = (-1,  0,  1, -1, 1, -1, 0, 1)
    DIST = tuple(pixel_m * math.hypot(dj, di) for dj, di in zip(DJ, DI))
    DIR = tuple(math.atan2(-dj, di) for dj, di in zip(DJ, DI))  # math angle of dir to neighbor

    # convert ig0 mask to seed list
    pq: list = []
    seeds = np.argwhere(ig0.astype(bool))
    for j, i in seeds:
        arrival[j, i] = 0.0
        heapq.heappush(pq, (0.0, int(j), int(i)))

    while pq:
        t, j, i = heapq.heappop(pq)
        if t > arrival[j, i]:
            continue
        Rh = R_head_mpmin[j, i]
        if Rh <= 0:
            continue
        lb = LB[j, i]
        wd = heading_rad[j, i]
        for k in range(8):
            jj = j + DJ[k]; ii = i + DI[k]
            if jj < 0 or jj >= H or ii < 0 or ii >= W:
                continue
            if not burnable[jj, ii]:
                continue
            theta = DIR[k] - wd
            sint = math.sin(theta); cost = math.cos(theta)
            denom = math.sqrt(lb*lb*sint*sint + cost*cost)
            R = Rh / denom if denom > 0 else 0.0
            if R <= 0:
                continue
            dt_s = (DIST[k] / R) * 60.0
            new_t = t + dt_s
            if new_t < arrival[jj, ii]:
                arrival[jj, ii] = new_t
                heapq.heappush(pq, (new_t, jj, ii))
    return arrival


# ----------------------------------------------------------------------
def run(cube: Cube, day0: datetime, n_days: int) -> dict[str, str]:
    grid = cube.grid

    # --- pull static layers
    fbfm = get_static(cube, "fbfm40")
    burnable = get_static(cube, "burnable").astype(bool)
    ig0 = get_static(cube, "ignition_t0").astype(bool)
    slope_deg = get_static(cube, "slope_deg")
    slope_tan = np.tan(np.deg2rad(np.clip(slope_deg, 0, 75)))

    # live moistures from cube (LFMC %) -> fraction
    lfmc = get_static(cube, "lfmc_pct").astype(np.float32) / 100.0
    # m_lh = LFMC fraction; m_lw = LFMC fraction (same proxy for shrub)
    m_lh = lfmc
    m_lw = lfmc

    # --- pull weather
    ws, wd_deg, rh, t_c, dfm1, dfm10, dfm100 = _aggregate_weather(cube, day0, n_days)

    # --- fuel parameter arrays
    fuel = fuel_arrays(fbfm)

    # --- wind reduction to midflame height
    sheltered = (fbfm >= 161) & (fbfm <= 189)   # TU + TL ~ canopy-sheltered
    waf = _midflame_factor(fuel["depth_m"], sheltered)
    U_mid = ws * waf

    # --- Rothermel
    R_head, LB = rothermel_R(fuel, dfm1, dfm10, dfm100, m_lh, m_lw,
                              U_mid, slope_tan)
    # mask non-burnable to be safe
    R_head = np.where(burnable & fuel["burnable"], R_head, 0.0).astype(np.float32)
    LB = np.where(R_head > 0, LB, 1.0).astype(np.float32)

    heading = _wind_to_heading_math_rad(wd_deg)

    # write static spread fields
    cube.write_static("R_head", R_head, source="Rothermel1972/Albini1976",
                      native_res_m=cube.grid.pixel_m, units="m/min",
                      producer="fire_spread",
                      description="Heading rate of spread (time-averaged weather)")
    cube.write_static("LB", LB, source="Anderson1983",
                      native_res_m=cube.grid.pixel_m, units="-",
                      producer="fire_spread",
                      description="Length-to-breadth ratio of fire ellipse")

    # --- Dijkstra
    print(f"      fast-marching on {grid.height} x {grid.width} grid")
    arr_s = _compute_arrival(R_head, LB, heading, ig0, burnable & fuel["burnable"],
                             grid.pixel_m)
    arr_s_out = np.where(np.isfinite(arr_s), arr_s, np.float32(-1.0)).astype(np.float32)
    cube.write_static("arrival_s", arr_s_out,
                      source="anisotropic Dijkstra fast-marching",
                      native_res_m=cube.grid.pixel_m, units="s",
                      producer="fire_spread",
                      description="Time of arrival of fire (-1 = never)")

    # --- hourly fire frames (binary mask) -- batch write
    print(f"      writing hourly fire frames ({n_days*24+1})")
    n_h = n_days * 24
    ts_fire = [day0 + timedelta(hours=hr) for hr in range(n_h + 1)]
    fire_3d = np.zeros((n_h + 1, grid.height, grid.width), dtype=np.uint8)
    finite = np.isfinite(arr_s)
    for hr, t in enumerate(ts_fire):
        thresh = hr * 3600.0
        fire_3d[hr] = (finite & (arr_s <= thresh)).astype(np.uint8)
    cube.write_3d("fire", ts_fire, fire_3d,
                  source="from arrival_s",
                  native_res_m=cube.grid.pixel_m, units="bool",
                  producer="fire_spread",
                  description="Burning indicator at hourly cadence")
    return {"R_head": "R_head", "LB": "LB", "arrival_s": "arrival_s",
            "fire": "fire"}
