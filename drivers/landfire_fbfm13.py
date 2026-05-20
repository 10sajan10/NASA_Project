"""LandfireFBFM13Driver: LANDFIRE 2024 Anderson-13 fuel categories
via the public ArcGIS ImageServer REST API.

Fetches the LF2024 (`Landfire_LF240`) FBFM13 raster for the cube's
bounding box, sized to the simulation grid, reprojected on the server
side to the simulation CRS — same workflow as DEMDriver, just hitting
the LANDFIRE service.

Produces two cube variables:
  * `fbfm13`    raw Anderson 13 codes (1..13 burnable; 91 urban,
                92 snow/ice, 93 barren, 98 water, 99 nodata)
  * `nfuel_cat` WRF-Fire-style mapping (1..13 burnable + 14 no-fuel)

The user previously rejected the bundled local LANDFIRE TIFs as a
"wrong fuel model". This driver pulls the *same* Anderson 13 product
from the network; the underlying classification is identical. If you
need a different fuel-model source, swap this driver for one of your
own; the engine doesn't care.
"""
from __future__ import annotations

import hashlib
import io
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
import requests

from cube.store import Cube
from drivers.base import Driver
from engine.log import get_logger



_log = get_logger(__name__)

# Latest LANDFIRE Anderson 13 surface fire behavior fuel model service.
# USGS migrated services in 2025-2026: the old landfire.cr.usgs.gov host
# now 404s. The current REST root is at lfps.usgs.gov; LF2024 lives under
# /Landfire_LF2024/LF2024_FBFM13_{CONUS,AK,HI}. If LANDFIRE rotates the
# path again, override via the constructor's `service_url` kwarg.
LANDFIRE_LF240_FBFM13 = (
    "https://lfps.usgs.gov/arcgis/rest/services/"
    "Landfire_LF2024/LF2024_FBFM13_CONUS/ImageServer/exportImage"
)

# LANDFIRE's ImageServer rejects very large pixel requests; tile if needed.
_DEFAULT_MAX_TILE_PIX = 2000
_DEFAULT_MIN_TILE_PIX = 250

# Anderson 13 non-burnable / nodata codes mapped to the WRF-Fire no-fuel
# sentinel category in `nfuel_cat`.
_NONBURNABLE_FBFM13 = {91, 92, 93, 98, 99}
_NODATA_SENTINEL = -9999


class LandfireFBFM13Driver(Driver):
    name = "landfire_fbfm13"
    produces = ["fbfm13", "nfuel_cat"]
    is_static = True

    def __init__(self,
                 *,
                 service_url: str = LANDFIRE_LF240_FBFM13,
                 cache_dir: str | Path = "data/raw/landfire_fbfm13",
                 max_tile_px: int = _DEFAULT_MAX_TILE_PIX,
                 min_tile_px: int = _DEFAULT_MIN_TILE_PIX,
                 retries: int = 4,
                 no_fuel_category: int = 14) -> None:
        self.service_url = service_url
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_tile_px = max(1, int(max_tile_px))
        self.min_tile_px = max(1, int(min_tile_px))
        self.retries = max(1, int(retries))
        self.no_fuel_category = int(no_fuel_category)

    # -- caching ----------------------------------------------------------
    def _cache_path(self, xmin: float, ymin: float, xmax: float, ymax: float,
                    width: int, height: int, sr: int) -> Path:
        key = (
            f"{sr}|{width}|{height}|"
            f"{xmin:.3f}|{ymin:.3f}|{xmax:.3f}|{ymax:.3f}"
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"lf240_fbfm13_{digest}.npy"

    # -- fitting / sizing -------------------------------------------------
    def _fit_tile(self, tile: np.ndarray, width: int, height: int
                  ) -> np.ndarray:
        tile = tile.astype(np.int16)
        if tile.shape == (height, width):
            return tile
        out = np.full((height, width), _NODATA_SENTINEL, dtype=np.int16)
        sh = min(tile.shape[0], height)
        sw = min(tile.shape[1], width)
        out[:sh, :sw] = tile[:sh, :sw]
        return out

    # -- network ----------------------------------------------------------
    def _request_once(self, params: dict[str, str],
                      width: int, height: int) -> np.ndarray:
        r = requests.get(self.service_url, params=params, timeout=(10, 180))
        r.raise_for_status()
        with rasterio.open(io.BytesIO(r.content)) as ds:
            arr = ds.read(1)
        return self._fit_tile(arr, width, height)

    def _request_tile(self, xmin: float, ymin: float,
                      xmax: float, ymax: float,
                      width: int, height: int, sr: int) -> np.ndarray:
        cache_path = self._cache_path(xmin, ymin, xmax, ymax,
                                       width, height, sr)
        if cache_path.exists():
            try:
                cached = np.load(cache_path)
                if cached.shape == (height, width):
                    return cached.astype(np.int16)
            except Exception:
                pass

        params = {
            "bbox":         f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR":       str(sr),
            "imageSR":      str(sr),
            "size":         f"{width},{height}",
            "format":       "tiff",
            "pixelType":    "U16",      # Anderson 13 codes are positive ints
            "noData":       "99",
            "interpolation": "RSP_NearestNeighbor",
            "f":            "image",
        }
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                arr = self._request_once(params, width, height)
                np.save(cache_path, arr)
                return arr
            except Exception as exc:
                last_exc = exc
                if attempt == self.retries - 1:
                    break
                time.sleep(2 ** attempt)

        # If the single request keeps failing, split the tile and recurse.
        can_split = width > self.min_tile_px or height > self.min_tile_px
        if can_split and (width > 1 or height > 1):
            _log.info(f"[landfire_fbfm13] tile request failed; splitting "
                  f"{width}x{height} tile into smaller requests "
                  f"({last_exc})")
            arr = self._request_split_tile(
                xmin, ymin, xmax, ymax, width, height, sr)
            np.save(cache_path, arr)
            return arr

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable")

    def _request_split_tile(self, xmin: float, ymin: float,
                            xmax: float, ymax: float,
                            width: int, height: int, sr: int) -> np.ndarray:
        out = np.full((height, width), _NODATA_SENTINEL, dtype=np.int16)
        x_mid = max(1, width // 2)
        y_mid = max(1, height // 2)
        x_edges = sorted(set([0, x_mid, width]))
        y_edges = sorted(set([0, y_mid, height]))
        pixel_x = (xmax - xmin) / width
        pixel_y = (ymax - ymin) / height

        for y0, y1 in zip(y_edges[:-1], y_edges[1:]):
            for x0, x1 in zip(x_edges[:-1], x_edges[1:]):
                sw = x1 - x0
                sh = y1 - y0
                if sw <= 0 or sh <= 0:
                    continue
                sub_xmin = xmin + x0 * pixel_x
                sub_xmax = xmin + x1 * pixel_x
                sub_ymax = ymax - y0 * pixel_y
                sub_ymin = ymax - y1 * pixel_y
                out[y0:y1, x0:x1] = self._request_tile(
                    sub_xmin, sub_ymin, sub_xmax, sub_ymax, sw, sh, sr)
        return out

    # -- engine entrypoint -----------------------------------------------
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        H, W = grid.height, grid.width
        sr = int(grid.crs_epsg)

        fbfm13 = np.full((H, W), _NODATA_SENTINEL, dtype=np.int16)

        # Tile requests so a single public-service hiccup doesn't kill a
        # multi-thousand-pixel grid.
        n_rows = math.ceil(H / self.max_tile_px)
        n_cols = math.ceil(W / self.max_tile_px)
        n_tiles = n_rows * n_cols
        tile_no = 0
        for j0 in range(0, H, self.max_tile_px):
            j1 = min(j0 + self.max_tile_px, H)
            for i0 in range(0, W, self.max_tile_px):
                i1 = min(i0 + self.max_tile_px, W)
                tile_no += 1
                tw = i1 - i0
                th = j1 - j0
                xmin = grid.x0 + i0 * grid.pixel_m
                xmax = grid.x0 + i1 * grid.pixel_m
                ymax = grid.y1 - j0 * grid.pixel_m
                ymin = grid.y1 - j1 * grid.pixel_m
                _log.info(f"[landfire_fbfm13] tile {tile_no}/{n_tiles}: "
                      f"rows {j0}:{j1}, cols {i0}:{i1}, size={tw}x{th}")
                tile = self._request_tile(
                    xmin, ymin, xmax, ymax, tw, th, sr)
                fbfm13[j0:j1, i0:i1] = self._fit_tile(tile, tw, th)

        # Burnable cells stay; non-burnable codes + nodata collapse to the
        # WRF-Fire no-fuel sentinel.
        burnable = (fbfm13 >= 1) & (fbfm13 <= 13)
        nfuel_cat = np.where(burnable, fbfm13,
                              self.no_fuel_category).astype(np.int16)

        src = "LANDFIRE_LF240_FBFM13_ImageServer"
        # Native LANDFIRE LF2024 resolution is 30 m; we sample at the cube's
        # pixel_m. The catalog records 30 so cube.satisfies sees us as a
        # 30-m source for downstream max_native_res_m checks.
        nat = 30.0
        cube.write_static(
            "fbfm13", fbfm13,
            source=src, native_res_m=nat, units="FBFM13_code",
            producer=self.name,
            description="Anderson 13 surface fire behavior fuel model "
                        "(LANDFIRE LF2024; 1..13 burnable, 91-99 non-"
                        "burnable, -9999 nodata)")
        cube.write_static(
            "nfuel_cat", nfuel_cat,
            source=src, native_res_m=nat, units="WRF_Fire_NFUEL_CAT",
            producer=self.name,
            description=f"WRF-Fire NFUEL_CAT: Anderson 1..13, "
                        f"{self.no_fuel_category} = no fuel")
        return list(self.produces)
