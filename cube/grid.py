"""SimulationGrid: fixed UTM grid, the canonical reference frame for one scenario.

All cube data lives on this grid. Drivers reproject native data here at ingest.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import json

import numpy as np
from pyproj import CRS, Transformer
from rasterio.transform import Affine


@dataclass
class SimulationGrid:
    crs_epsg: int
    pixel_m: float
    width: int
    height: int
    x0: float          # left edge UTM x
    y1: float          # top  edge UTM y

    @property
    def crs(self) -> CRS:
        return CRS.from_epsg(self.crs_epsg)

    @property
    def transform(self) -> Affine:
        return Affine(self.pixel_m, 0, self.x0, 0, -self.pixel_m, self.y1)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        x1 = self.x0 + self.width * self.pixel_m
        y0 = self.y1 - self.height * self.pixel_m
        return (self.x0, y0, x1, self.y1)

    def cell_centers_xy(self) -> tuple[np.ndarray, np.ndarray]:
        xs = self.x0 + (np.arange(self.width) + 0.5) * self.pixel_m
        ys = self.y1 - (np.arange(self.height) + 0.5) * self.pixel_m
        return xs, ys

    def lonlat_bbox(self) -> tuple[float, float, float, float]:
        xmin, ymin, xmax, ymax = self.bounds
        fwd = Transformer.from_crs(self.crs, 4326, always_xy=True).transform
        xs = [xmin, xmin, xmax, xmax]
        ys = [ymin, ymax, ymin, ymax]
        lons, lats = fwd(xs, ys)
        return (float(min(lons)), float(min(lats)),
                float(max(lons)), float(max(lats)))

    @classmethod
    def from_center_radius(cls, lon: float, lat: float, radius_m: float,
                           pixel_m: float = 100.0) -> "SimulationGrid":
        zone = int((lon + 180) / 6) + 1
        epsg = 32600 + zone if lat >= 0 else 32700 + zone
        crs = CRS.from_epsg(epsg)
        fwd = Transformer.from_crs(4326, crs, always_xy=True).transform
        cx, cy = fwd(lon, lat)
        x0 = float(np.floor((cx - radius_m) / pixel_m) * pixel_m)
        x1 = float(np.ceil((cx + radius_m) / pixel_m) * pixel_m)
        y0 = float(np.floor((cy - radius_m) / pixel_m) * pixel_m)
        y1 = float(np.ceil((cy + radius_m) / pixel_m) * pixel_m)
        width = int((x1 - x0) / pixel_m)
        height = int((y1 - y0) / pixel_m)
        return cls(crs_epsg=epsg, pixel_m=pixel_m, width=width, height=height,
                   x0=x0, y1=y1)

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load(cls, path: Path | str) -> "SimulationGrid":
        return cls(**json.loads(Path(path).read_text()))
