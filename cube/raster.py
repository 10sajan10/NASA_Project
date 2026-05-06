"""GeoTIFF I/O and clip-reproject helpers."""
from __future__ import annotations
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import reproject, Resampling
from pyproj import CRS


def write_geotiff(path: Path | str, array: np.ndarray, transform_affine: tuple,
                  crs: CRS, nodata: float | None = None) -> None:
    a, b, c, d, e, f = transform_affine
    tr = Affine(a, b, c, d, e, f)
    if array.ndim == 2:
        h, w = array.shape
        count = 1
    else:
        count, h, w = array.shape
    with rasterio.open(
        path, "w",
        driver="GTiff",
        height=h, width=w, count=count,
        dtype=array.dtype,
        crs=crs.to_wkt(),
        transform=tr,
        nodata=nodata,
        compress="deflate", tiled=True,
    ) as dst:
        if array.ndim == 2:
            dst.write(array, 1)
        else:
            dst.write(array)


def clip_raster_to_grid(src_path: Path | str, out_path: Path | str,
                        bounds: tuple[float, float, float, float],
                        dst_crs: CRS,
                        pixel_m: float,
                        resampling: Resampling = Resampling.nearest) -> None:
    """Reproject + clip a source raster to (bounds, dst_crs, pixel_m).

    bounds = (xmin, ymin, xmax, ymax) in dst_crs units.
    """
    xmin, ymin, xmax, ymax = bounds
    width = int(np.ceil((xmax - xmin) / pixel_m))
    height = int(np.ceil((ymax - ymin) / pixel_m))
    dst_transform = Affine(pixel_m, 0, xmin, 0, -pixel_m, ymax)

    with rasterio.open(src_path) as src:
        profile = src.profile.copy()
        profile.update(
            crs=dst_crs.to_wkt(),
            transform=dst_transform,
            width=width, height=height,
            compress="deflate", tiled=True,
        )
        with rasterio.open(out_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=resampling,
                )
