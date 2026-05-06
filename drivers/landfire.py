"""LandfireDriver: read the local LANDFIRE FBFM40 GeoTIFF and reproject+clip
it onto the simulation grid. NoData is mapped to 91 (urban / non-burnable)
to keep the fire model from walking into invalid cells.
"""
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


class LandfireDriver(Driver):
    name = "landfire"
    produces = ["fbfm40"]
    is_static = True

    def __init__(self, src_path: str | Path):
        self.src_path = str(src_path)

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)
        dst = np.zeros(grid.shape, dtype=np.int16)

        with rasterio.open(self.src_path) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=grid.crs,
                resampling=Resampling.nearest,
            )
            native = float(abs(src.transform.a))

        # remap nodata-ish values to FBFM40 NB1 (urban, non-burnable)
        # so missing pixels don't break the spread model
        dst = np.where((dst <= 0) | (dst > 204), 91, dst).astype(np.int16)

        cube.write_static(
            "fbfm40", dst,
            source=f"LANDFIRE_LF2024:{Path(self.src_path).name}",
            native_res_m=native,
            units="FBFM40_code",
            producer=self.name,
            description="Scott & Burgan 40 surface fire behavior fuel model code")
        return ["fbfm40"]
