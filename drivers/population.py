"""Population raster adapter."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling

from cube.store import Cube
from drivers.base import Driver


class PopulationRasterDriver(Driver):
    name = "population"
    produces = ["population"]
    is_static = True

    def __init__(self, src_path: str | Path, *,
                 source_name: str = "population_raster"):
        self.src_path = Path(src_path)
        self.source_name = source_name

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if not self.src_path.exists():
            raise FileNotFoundError(self.src_path)
        grid = cube.grid
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)
        dst = np.zeros(grid.shape, dtype=np.float32)
        with rasterio.open(self.src_path) as src:
            resampling = getattr(Resampling, "sum", Resampling.bilinear)
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=grid.crs,
                resampling=resampling,
            )
            native = float(abs(src.transform.a))
        dst = np.nan_to_num(dst, nan=0.0, posinf=0.0, neginf=0.0)
        dst = np.maximum(dst, 0.0).astype(np.float32)
        cube.write_static("population", dst, source=self.source_name,
                          native_res_m=native, units="people",
                          producer=self.name,
                          description="Population count per simulation cell")
        return ["population"]
