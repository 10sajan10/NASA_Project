"""DEMDriver: USGS 3DEP elevation via the public ImageServer REST API.

We request a GeoTIFF in the simulation CRS, sized to the simulation grid,
so no client-side reprojection is needed. Slope and aspect are derived
in-process and written to the cube.
"""
from __future__ import annotations
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

# 3DEP server caps both dimensions at 4100 px per request.
_MAX_PIX = 4000


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

    def __init__(self, cache_dir: str | Path = "data/raw/dem"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _request_tile(self, xmin: float, ymin: float, xmax: float, ymax: float,
                      width: int, height: int, sr: int) -> np.ndarray:
        params = {
            "bbox": f"{xmin},{ymin},{xmax},{ymax}",
            "bboxSR": str(sr),
            "imageSR": str(sr),
            "size": f"{width},{height}",
            "format": "tiff",
            "pixelType": "F32",
            "f": "image",
        }
        for attempt in range(4):
            try:
                r = requests.get(USGS_3DEP, params=params, timeout=120)
                r.raise_for_status()
                with rasterio.open(io.BytesIO(r.content)) as ds:
                    arr = ds.read(1).astype(np.float32)
                return arr
            except Exception as exc:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        H, W = grid.height, grid.width
        sr = int(grid.crs_epsg)

        dem = np.zeros((H, W), dtype=np.float32)

        # tile to stay under the 4100x4100 cap
        for j0 in range(0, H, _MAX_PIX):
            j1 = min(j0 + _MAX_PIX, H)
            for i0 in range(0, W, _MAX_PIX):
                i1 = min(i0 + _MAX_PIX, W)
                tw = i1 - i0; th = j1 - j0
                xmin = grid.x0 + i0 * grid.pixel_m
                xmax = grid.x0 + i1 * grid.pixel_m
                ymax = grid.y1 - j0 * grid.pixel_m
                ymin = grid.y1 - j1 * grid.pixel_m
                tile = self._request_tile(xmin, ymin, xmax, ymax, tw, th, sr)
                if tile.shape != (th, tw):
                    # server may return slightly off; pad/trim
                    sh = min(tile.shape[0], th)
                    sw = min(tile.shape[1], tw)
                    dem[j0:j0+sh, i0:i0+sw] = tile[:sh, :sw]
                else:
                    dem[j0:j1, i0:i1] = tile

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
