"""SentinelDriver: NDVI / NDWI from Sentinel-2 L2A on Microsoft Planetary
Computer.

Strategy (future-dated scenarios):
  - If scenario_date is past the data record, search the same calendar month
    in the most recent full year that has data.
  - Otherwise search [scenario_date - 60 d, scenario_date].
  - Filter scenes by cloud cover < `max_cloud_pct`.
  - Per scene: load B04 (red 10 m), B08 (NIR 10 m), B11 (SWIR1 20 m), and
    SCL (scene-classification 20 m). Reproject onto the simulation grid
    using nearest neighbour (bands are spectrally banded; bilinear introduces
    fractional class artefacts in SCL).
  - Apply the SCL cloud mask: keep classes 4 (vegetation), 5 (bare soil),
    6 (water), 7 (unclassified), 11 (snow). Drop 0 (NoData), 1 (saturated),
    2 (shadow), 3 (cloud shadow), 8/9/10 (clouds + cirrus).
  - Mosaic by per-pixel mean over valid observations. Write NDVI and NDWI
    as static fields, since per-pixel temporal trend extrapolation is a
    larger model that we layer on top later.

Indices:
  NDVI = (B08 - B04) / (B08 + B04)
  NDWI = (B08 - B11) / (B08 + B11)        [Gao 1996]
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject

import planetary_computer as pc
from pystac_client import Client

from cube.store import Cube
from drivers.base import Driver

PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
VALID_SCL = {4, 5, 6, 7, 11}
DATA_END = datetime(2025, 1, 1)  # treat anything past this as "future"


class SentinelDriver(Driver):
    name = "sentinel2"
    produces = ["ndvi", "ndwi"]
    is_static = True

    def __init__(self, scenario_date: Optional[datetime] = None,
                 max_cloud_pct: float = 30.0,
                 max_scenes: int = 12,
                 cache_dir: str | Path = "data/raw/sentinel"):
        self.scenario_date = scenario_date or datetime.now(timezone.utc)
        self.max_cloud_pct = max_cloud_pct
        self.max_scenes = max_scenes
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _search_window(self) -> tuple[datetime, datetime]:
        sd = self.scenario_date
        if sd >= DATA_END:
            ref_year = DATA_END.year - 1
            t0 = datetime(ref_year, sd.month, 1)
            # end of month
            if sd.month == 12:
                t1 = datetime(ref_year + 1, 1, 1) - timedelta(days=1)
            else:
                t1 = datetime(ref_year, sd.month + 1, 1) - timedelta(days=1)
            return t0, t1
        return sd - timedelta(days=60), sd

    def _stac_search(self, bbox_lonlat: tuple) -> list:
        client = Client.open(PC_STAC, modifier=pc.sign_inplace)
        t0, t1 = self._search_window()
        search = client.search(
            collections=[COLLECTION],
            bbox=list(bbox_lonlat),
            datetime=f"{t0.strftime('%Y-%m-%d')}/{t1.strftime('%Y-%m-%d')}",
            query={"eo:cloud_cover": {"lt": self.max_cloud_pct}},
            limit=200,
        )
        items = list(search.items())
        items.sort(key=lambda it: float(it.properties.get("eo:cloud_cover", 100)))
        return items[: self.max_scenes]

    def _read_band_to_grid(self, href: str, grid, dtype=np.float32,
                           resampling: Resampling = Resampling.nearest
                           ) -> np.ndarray:
        dst_transform = Affine(grid.pixel_m, 0, grid.x0,
                               0, -grid.pixel_m, grid.y1)
        dst = np.zeros(grid.shape, dtype=dtype)
        with rasterio.open(href) as src:
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=dst_transform, dst_crs=grid.crs,
                resampling=resampling,
            )
        return dst

    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        bbox = grid.lonlat_bbox()
        items = self._stac_search(bbox)
        if not items:
            raise RuntimeError("no Sentinel-2 scenes found for grid + window")

        ndvi_sum = np.zeros(grid.shape, dtype=np.float32)
        ndwi_sum = np.zeros(grid.shape, dtype=np.float32)
        ndvi_n = np.zeros(grid.shape, dtype=np.int32)
        ndwi_n = np.zeros(grid.shape, dtype=np.int32)

        for k, item in enumerate(items):
            try:
                b04_href = item.assets["B04"].href
                b08_href = item.assets["B08"].href
                b11_href = item.assets["B11"].href
                scl_href = item.assets["SCL"].href
            except KeyError:
                continue

            try:
                scl = self._read_band_to_grid(scl_href, grid, dtype=np.uint8,
                                              resampling=Resampling.nearest)
                valid = np.isin(scl, list(VALID_SCL))
                if valid.sum() == 0:
                    continue
                b04 = self._read_band_to_grid(b04_href, grid, dtype=np.float32,
                                              resampling=Resampling.bilinear)
                b08 = self._read_band_to_grid(b08_href, grid, dtype=np.float32,
                                              resampling=Resampling.bilinear)
                b11 = self._read_band_to_grid(b11_href, grid, dtype=np.float32,
                                              resampling=Resampling.bilinear)
            except Exception as exc:
                print(f"      skip scene {item.id}: {exc}")
                continue

            with np.errstate(divide="ignore", invalid="ignore"):
                ndvi_i = (b08 - b04) / (b08 + b04)
                ndwi_i = (b08 - b11) / (b08 + b11)
            good_v = valid & np.isfinite(ndvi_i) & (b04 + b08 > 0)
            good_w = valid & np.isfinite(ndwi_i) & (b08 + b11 > 0)
            ndvi_sum[good_v] += ndvi_i[good_v]
            ndvi_n[good_v] += 1
            ndwi_sum[good_w] += ndwi_i[good_w]
            ndwi_n[good_w] += 1
            print(f"      scene {k+1}/{len(items)}  cloud="
                  f"{item.properties.get('eo:cloud_cover'):.1f}%  "
                  f"valid_pixels={int(valid.sum())}")

        ndvi = np.where(ndvi_n > 0, ndvi_sum / np.maximum(ndvi_n, 1), np.nan)
        ndwi = np.where(ndwi_n > 0, ndwi_sum / np.maximum(ndwi_n, 1), np.nan)
        ndvi = ndvi.astype(np.float32)
        ndwi = ndwi.astype(np.float32)

        src = (f"Sentinel-2 L2A (Planetary Computer); "
               f"window {self._search_window()[0].date()}.."
               f"{self._search_window()[1].date()}; "
               f"<{self.max_cloud_pct}% cloud; mean of "
               f"{int(ndvi_n.max())}.. observations")
        cube.write_static("ndvi", ndvi, source=src, native_res_m=10.0,
                          units="dimensionless", producer=self.name,
                          description="NDVI = (NIR-RED)/(NIR+RED)")
        cube.write_static("ndwi", ndwi, source=src, native_res_m=10.0,
                          units="dimensionless", producer=self.name,
                          description="NDWI = (NIR-SWIR1)/(NIR+SWIR1) [Gao 1996]")
        return ["ndvi", "ndwi"]
