"""Landsat historical spectral-index adapter.

This driver fetches Landsat Collection 2 Level-2 scenes from Microsoft
Planetary Computer for the target season, computes NDVI / NDWI / NBR on the
simulation grid, and stores the historical index stacks in the cube. A
separate model predicts future scenario-date indices from those stacks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import planetary_computer as pc
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine
from rasterio.warp import reproject
from pystac_client import Client

from cube.store import Cube
from drivers.base import Driver


PC_STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"


class LandsatHistoryDriver(Driver):
    name = "landsat_history"
    produces = ["landsat_ndvi_hist", "landsat_ndwi_hist", "landsat_nbr_hist"]
    is_static = False

    def __init__(self, *, target_date: datetime,
                 years_back: int = 12,
                 day_window: int = 30,
                 max_cloud_pct: float = 50.0,
                 max_scenes_per_year: int = 3,
                 cache_dir: str | Path = "data/raw/landsat"):
        self.target_date = target_date
        self.years_back = years_back
        self.day_window = day_window
        self.max_cloud_pct = max_cloud_pct
        self.max_scenes_per_year = max_scenes_per_year
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _history_years(self) -> list[int]:
        now = datetime.now(timezone.utc).year
        latest = min(now - 1, self.target_date.year - 1)
        latest = max(latest, 2013)
        first = max(2013, latest - self.years_back + 1)
        return list(range(first, latest + 1))

    def _date_in_year(self, year: int) -> datetime:
        day = min(self.target_date.day, 28 if self.target_date.month == 2 else 31)
        return datetime(year, self.target_date.month, day)

    def _stac_items(self, bbox_lonlat: tuple[float, float, float, float]) -> list:
        client = Client.open(PC_STAC, modifier=pc.sign_inplace)
        items = []
        for year in self._history_years():
            center = self._date_in_year(year)
            t0 = center - timedelta(days=self.day_window)
            t1 = center + timedelta(days=self.day_window)
            search = client.search(
                collections=[COLLECTION],
                bbox=list(bbox_lonlat),
                datetime=f"{t0:%Y-%m-%d}/{t1:%Y-%m-%d}",
                query={"eo:cloud_cover": {"lt": self.max_cloud_pct}},
                limit=100,
            )
            year_items = list(search.items())
            year_items.sort(key=lambda it: float(
                it.properties.get("eo:cloud_cover", 100)))
            items.extend(year_items[: self.max_scenes_per_year])
        items.sort(key=lambda it: it.datetime or datetime.min.replace(
            tzinfo=timezone.utc))
        return items

    def _read_band_to_grid(self, href: str, grid, dtype=np.float32,
                           resampling: Resampling = Resampling.bilinear
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

    @staticmethod
    def _scale_sr(arr: np.ndarray) -> np.ndarray:
        # Landsat C2 L2 surface reflectance scale/offset.
        return arr.astype(np.float32) * 0.0000275 - 0.2

    @staticmethod
    def _qa_valid(qa: np.ndarray) -> np.ndarray:
        # QA_PIXEL: fill, dilated cloud, cirrus, cloud, shadow, snow.
        bad_bits = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5)
        return (qa.astype(np.uint16) & bad_bits) == 0

    @staticmethod
    def _index(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            out = (a - b) / (a + b)
        return np.where(np.isfinite(out), out, np.nan).astype(np.float32)

    # --------------------------------------------------------------------
    # Tile-streamed write: each scene is read into a single full-grid array,
    # immediately written to its own time slice in a chunked Zarr store, and
    # released. We never hold the whole (n_scenes, H, W) stack in memory.
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        grid = cube.grid
        items = self._stac_items(grid.lonlat_bbox())
        if not items:
            raise RuntimeError("no Landsat scenes found for grid + seasonal window")

        # First pass: collect the timestamps of items that actually have all
        # required assets, so we can pre-allocate the Zarr store with the
        # correct length. We don't do I/O here.
        usable: list[tuple[int, datetime, "object"]] = []
        for k, item in enumerate(items, start=1):
            assets = item.assets
            needed = ["red", "nir08", "swir16", "swir22", "qa_pixel"]
            if any(name not in assets for name in needed):
                continue
            dt = item.datetime
            if dt is None:
                dt = datetime.fromisoformat(item.properties["datetime"])
            usable.append((k, dt.replace(tzinfo=None), item))
        if not usable:
            raise RuntimeError("no Landsat scenes had all required bands")

        n_scenes = len(usable)
        ts_planned = [t for _, t, _ in usable]
        src = (f"Landsat C2 L2 Planetary Computer; "
               f"{min(ts_planned).date()}..{max(ts_planned).date()}; "
               f"target season {self.target_date.month:02d}-"
               f"{self.target_date.day:02d} +/- {self.day_window}d; "
               "scene-streamed")

        H, W = grid.shape
        # one chunk per scene-time, modest spatial chunks so per-tile
        # regression downstream is fast and memory-bounded
        spatial_chunk = 256
        for var, descr in [
            ("landsat_ndvi_hist", "Historical Landsat NDVI seasonal samples"),
            ("landsat_ndwi_hist", "Historical Landsat NDWI seasonal samples"),
            ("landsat_nbr_hist",  "Historical Landsat NBR seasonal samples"),
        ]:
            cube.init_time_tiled(
                var, ts=ts_planned, dtype="float32", fill_value=np.nan,
                source=src, native_res_m=30.0, units="dimensionless",
                producer=self.name, description=descr,
                chunk=(1, min(spatial_chunk, H), min(spatial_chunk, W)))

        # Stream scenes one at a time
        scene_idx = 0
        kept = 0
        for orig_k, dt, item in usable:
            scene_idx += 1
            assets = item.assets
            try:
                qa = self._read_band_to_grid(
                    assets["qa_pixel"].href, grid, dtype=np.uint16,
                    resampling=Resampling.nearest)
                valid = self._qa_valid(qa)
                if valid.sum() == 0:
                    print(f"      skip Landsat scene {orig_k}/{n_scenes}: no valid pixels")
                    continue
                red = self._scale_sr(self._read_band_to_grid(assets["red"].href, grid))
                nir = self._scale_sr(self._read_band_to_grid(assets["nir08"].href, grid))
                sw1 = self._scale_sr(self._read_band_to_grid(assets["swir16"].href, grid))
                sw2 = self._scale_sr(self._read_band_to_grid(assets["swir22"].href, grid))
            except Exception as exc:
                print(f"      skip Landsat scene {item.id}: {exc}")
                continue

            ndvi = np.where(valid, self._index(nir, red), np.nan).astype(np.float32)
            ndwi = np.where(valid, self._index(nir, sw1), np.nan).astype(np.float32)
            nbr  = np.where(valid, self._index(nir, sw2), np.nan).astype(np.float32)
            del red, nir, sw1, sw2, qa

            n_finite = int(np.isfinite(ndvi).sum())
            if n_finite == 0:
                continue
            kept += 1

            t_slice = slice(scene_idx - 1, scene_idx)
            full_y = slice(0, H); full_x = slice(0, W)
            cube.write_chunk_time("landsat_ndvi_hist", t_slice, full_y, full_x,
                                   ndvi[None])
            cube.write_chunk_time("landsat_ndwi_hist", t_slice, full_y, full_x,
                                   ndwi[None])
            cube.write_chunk_time("landsat_nbr_hist",  t_slice, full_y, full_x,
                                   nbr[None])
            print(f"      Landsat scene {orig_k}/{n_scenes}  cloud="
                  f"{item.properties.get('eo:cloud_cover', float('nan')):.1f}%  "
                  f"valid_pixels={n_finite}")
            del ndvi, ndwi, nbr, valid

        if kept == 0:
            raise RuntimeError(
                "Landsat scenes found, but no valid pixels remained")
        print(f"      kept {kept}/{n_scenes} scenes")
        return list(self.produces)
