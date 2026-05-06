"""DEMDriver: USGS 3DEP elevation via the public ImageServer REST API.

We request a GeoTIFF in the simulation CRS, sized to the simulation grid,
so no client-side reprojection is needed. Slope and aspect are derived
in-process and written to the cube.
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

USGS_3DEP = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer/exportImage")

# 3DEP caps dimensions above ~4100 px, but large requests often time out on the
# public service. Start with 1000 px tiles and split failing tiles recursively.
_DEFAULT_MAX_TILE_PIX = 1000
_DEFAULT_MIN_TILE_PIX = 250


def _slope_aspect_deg(z: np.ndarray, pixel_m: float
                      ) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) 3x3 slope/aspect estimator, vectorised."""
    z = z.astype(np.float64)
    pad = np.pad(z, 1, mode="edge")
    a = pad[:-2, :-2]; b = pad[:-2, 1:-1]; c = pad[:-2, 2:]
    d = pad[1:-1, :-2];                    f = pad[1:-1, 2:]
    g = pad[2:, :-2]; h = pad[2:, 1:-1]; i = pad[2:, 2:]
    dzdx = ((c + 2*f + i) - (a + 2*d + g)) / (8 * pixel_m)
    dzdy = ((g + 2*h + i) - (a + 2*b + c)) / (8 * pixel_m)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    aspect = np.degrees(np.arctan2(dzdy, -dzdx))
    aspect = (aspect + 360.0) % 360.0
    aspect[(dzdx == 0) & (dzdy == 0)] = -1.0
    return slope.astype(np.float32), aspect.astype(np.float32)


class DEMDriver(Driver):
    name = "dem"
    produces = ["dem", "slope_deg", "aspect_deg"]
    is_static = True

    def __init__(self, cache_dir: str | Path = "data/raw/dem",
                 max_tile_px: int = _DEFAULT_MAX_TILE_PIX,
                 min_tile_px: int = _DEFAULT_MIN_TILE_PIX,
                 retries: int = 4):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_tile_px = max(1, int(max_tile_px))
        self.min_tile_px = max(1, int(min_tile_px))
        self.retries = max(1, int(retries))

    def _cache_path(self, xmin: float, ymin: float, xmax: float, ymax: float,
                    width: int, height: int, sr: int) -> Path:
        key = (
            f"{sr}|{width}|{height}|"
            f"{xmin:.3f}|{ymin:.3f}|{xmax:.3f}|{ymax:.3f}"
        )
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"usgs3dep_{digest}.npy"

    def _fit_tile(self, tile: np.ndarray, width: int, height: int) -> np.ndarray:
        tile = tile.astype(np.float32)
        if tile.shape == (height, width):
            return tile
        out = np.full((height, width), np.nan, dtype=np.float32)
        sh = min(tile.shape[0], height)
        sw = min(tile.shape[1], width)
        out[:sh, :sw] = tile[:sh, :sw]
        return out

    def _request_once(self, params: dict[str, str],
                      width: int, height: int) -> np.ndarray:
        r = requests.get(USGS_3DEP, params=params, timeout=(10, 120))
        r.raise_for_status()
        with rasterio.open(io.BytesIO(r.content)) as ds:
            arr = ds.read(1)
        return self._fit_tile(arr, width, height)

    def _request_tile(self, xmin: float, ymin: float, xmax: float, ymax: float,
                      width: int, height: int, sr: int) -> np.ndarray:
        cache_path = self._cache_path(xmin, ymin, xmax, ymax, width, height, sr)
        if cache_path.exists():
            try:
                cached = np.load(cache_path)
                if cached.shape == (height, width):
                    return cached.astype(np.float32)
            except Exception:
                pass

        params = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR": str(sr),
            "imageSR": str(sr),
            "size": f"{width},{height}",
            "format": "tiff",
            "pixelType": "F32",
            "f": "image",
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

        can_split = width > self.min_tile_px or height > self.min_tile_px
        if can_split and (width > 1 or height > 1):
            print("[dem] tile request failed; splitting "
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
        out = np.full((height, width), np.nan, dtype=np.float32)
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

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        H, W = grid.height, grid.width
        sr = int(grid.crs_epsg)

        dem = np.full((H, W), np.nan, dtype=np.float32)

        # Tile requests so public DEM service failures do not kill large runs.
        n_rows = math.ceil(H / self.max_tile_px)
        n_cols = math.ceil(W / self.max_tile_px)
        n_tiles = n_rows * n_cols
        tile_no = 0
        for j0 in range(0, H, self.max_tile_px):
            j1 = min(j0 + self.max_tile_px, H)
            for i0 in range(0, W, self.max_tile_px):
                i1 = min(i0 + self.max_tile_px, W)
                tile_no += 1
                tw = i1 - i0; th = j1 - j0
                xmin = grid.x0 + i0 * grid.pixel_m
                xmax = grid.x0 + i1 * grid.pixel_m
                ymax = grid.y1 - j0 * grid.pixel_m
                ymin = grid.y1 - j1 * grid.pixel_m
                print(f"[dem] tile {tile_no}/{n_tiles}: "
                      f"rows {j0}:{j1}, cols {i0}:{i1}, size={tw}x{th}")
                tile = self._request_tile(xmin, ymin, xmax, ymax, tw, th, sr)
                dem[j0:j1, i0:i1] = self._fit_tile(tile, tw, th)

        # USGS 3DEP returns NoData around -3.4e+38 (max -F32). Clean it up.
        bad = ~np.isfinite(dem) | (dem < -1e6) | (dem > 1e6)
        if bad.any():
            dem[bad] = np.nanmean(dem[~bad]) if (~bad).any() else 0.0

        slope, aspect = _slope_aspect_deg(dem, grid.pixel_m)

        src = "USGS_3DEP_ImageServer"
        nat = 10.0  # 3DEP best resolution; we sampled at grid.pixel_m
        cube.write_static("dem", dem.astype(np.float32),
                          source=src, native_res_m=nat,
                          units="m", producer=self.name,
                          description="USGS 3DEP terrain elevation")
        cube.write_static("slope_deg", slope,
                          source=src, native_res_m=nat,
                          units="deg", producer=self.name,
                          description="Horn (1981) slope from DEM")
        cube.write_static("aspect_deg", aspect,
                          source=src, native_res_m=nat,
                          units="deg", producer=self.name,
                          description="Horn aspect (deg from N, CW); -1 = flat")
        return list(self.produces)
