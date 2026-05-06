"""Future satellite-index prediction from historical seasonal samples."""
from __future__ import annotations

from datetime import datetime

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest


def _decimal_year(t: datetime) -> float:
    start = datetime(t.year, 1, 1)
    end = datetime(t.year + 1, 1, 1)
    return t.year + (t - start).total_seconds() / (end - start).total_seconds()


def _predict_linear(ts: list[datetime], arr: np.ndarray, target: datetime,
                    default: float = 0.0) -> np.ndarray:
    years = np.array([_decimal_year(t) for t in ts], dtype=np.float32)
    x = years - years.mean()
    xt = np.float32(_decimal_year(target) - years.mean())
    data = arr.astype(np.float32)
    mask = np.isfinite(data)
    n = mask.sum(axis=0).astype(np.float32)

    safe = np.where(mask, data, 0.0)
    sx = (mask * x[:, None, None]).sum(axis=0)
    sy = safe.sum(axis=0)
    sxx = (mask * (x[:, None, None] ** 2)).sum(axis=0)
    sxy = (safe * x[:, None, None]).sum(axis=0)
    den = n * sxx - sx * sx

    slope = np.zeros_like(sy, dtype=np.float32)
    good_fit = (n >= 2) & (np.abs(den) > 1e-6)
    slope[good_fit] = (n[good_fit] * sxy[good_fit]
                       - sx[good_fit] * sy[good_fit]) / den[good_fit]
    intercept = np.divide(sy - slope * sx, np.maximum(n, 1.0))
    pred = intercept + slope * xt

    climatology = np.nanmedian(data, axis=0)
    pred = np.where(n >= 2, pred, climatology)
    pred = np.where(np.isfinite(pred), pred, default)
    return np.clip(pred, -1.0, 1.0).astype(np.float32)


class SatelliteIndexTrendProducer(BaseProducer):
    name = "satellite_index_trend"
    produces = ["ndvi", "ndwi", "nbr"]
    requires = ["landsat_ndvi_hist", "landsat_ndwi_hist", "landsat_nbr_hist"]
    kind = "model"
    can_run_parallel = False

    def __init__(self, target_date: datetime):
        self.target_date = target_date

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        mapping = {
            "ndvi": ("landsat_ndvi_hist", 0.2),
            "ndwi": ("landsat_ndwi_hist", 0.0),
            "nbr": ("landsat_nbr_hist", 0.2),
        }
        for out_var, (hist_var, default) in mapping.items():
            ts, arr = cube.read_3d(hist_var)
            pred = _predict_linear(ts, arr, self.target_date, default=default)
            cube.write_static(
                out_var, pred,
                source=(f"linear trend on {hist_var}; target="
                        f"{self.target_date.date()}"),
                native_res_m=30.0, units="dimensionless",
                producer=self.name,
                description=f"Predicted future {out_var.upper()} from Landsat")
        return list(self.produces)
