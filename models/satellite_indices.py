"""Per-pixel multivariate regression for satellite indices.

Replaces the earlier "linear-trend over scenes" predictor. For every pixel,
fits the temporal model

    index(year, doy) = β₀ + β₁(year - ȳ)
                     + β₂ sin(2π·doy/365.25) + β₃ cos(2π·doy/365.25)
                     + β₄ sin(4π·doy/365.25) + β₅ cos(4π·doy/365.25)

against the historical Landsat stack and predicts the index at the scenario
date. The same regression is applied to NDVI / NDWI / NBR.

Outputs (static cube variables):
    ndvi, ndwi, nbr  -- predicted values at the scenario date
"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest
from models.temporal_regression import fit_and_predict


_SOURCES: dict[str, tuple[str, tuple[float, float], float]] = {
    # output_var : (history_var, clip_range, default_if_all_missing)
    "ndvi": ("landsat_ndvi_hist", (-1.0, 1.0), 0.20),
    "ndwi": ("landsat_ndwi_hist", (-1.0, 1.0), 0.00),
    "nbr":  ("landsat_nbr_hist",  (-1.0, 1.0), 0.20),
}


class SatelliteIndexRegression(BaseProducer):
    name = "satellite_index_regression"
    produces = list(_SOURCES.keys())
    requires = [v[0] for v in _SOURCES.values()]
    kind = "model"
    can_run_parallel = False

    def __init__(self, target_date: datetime, n_harmonics: int = 2):
        self.target_date = target_date
        self.n_harmonics = n_harmonics

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        for out_var, (hist_var, clip_range, default) in _SOURCES.items():
            ts, arr = cube.read_3d(hist_var)
            if not ts or arr.size == 0:
                raise RuntimeError(f"{hist_var} has no data; cannot fit")
            pred = fit_and_predict(
                ts_train=ts, arr=arr,
                ts_target=[self.target_date],
                n_harmonics=self.n_harmonics,
                clip=clip_range,
            )[0]
            # final NaN sweep — fall back to global median, then default
            if not np.all(np.isfinite(pred)):
                med = float(np.nanmedian(pred)) if np.isfinite(pred).any() else default
                pred = np.where(np.isfinite(pred), pred, med).astype(np.float32)
            cube.write_static(
                out_var, pred,
                source=(f"per-pixel multivariate regression on {hist_var} "
                        f"(year + {self.n_harmonics} harmonics); "
                        f"target = {self.target_date.date()}"),
                native_res_m=30.0, units="dimensionless",
                producer=self.name,
                description=(f"Predicted future {out_var.upper()} from a per-cell "
                             "multivariate regression of historical Landsat samples"),
            )
        return list(self.produces)
