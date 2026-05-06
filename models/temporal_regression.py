"""Per-pixel multivariate temporal regression.

Fits a single linear model per simulation cell of the form

    y(t) = β₀ + β₁·(year - ȳ)
            + Σ_{k=1..K} [ β_{2k}·sin(2π·k·doy/365.25)
                          + β_{2k+1}·cos(2π·k·doy/365.25) ]

K is the number of seasonal harmonics (default 2 → annual + semi-annual).
The regression captures inter-annual trend (climate change / drought
recovery) and within-year seasonality. Solved by OLS, vectorised across
all pixels in one `np.linalg.lstsq` call so a 1000² grid with ~100 historical
samples runs in seconds rather than hours.

Used by the satellite-index and ERA5-climate models.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np


@dataclass
class TemporalRegressionFit:
    """Coefficients + design metadata; serialisable + reusable for prediction."""
    coef: np.ndarray            # (n_features, H, W)
    n_harmonics: int
    year_centre: float
    feature_names: list[str]

    @property
    def n_features(self) -> int:
        return self.coef.shape[0]


def _decimal_year(t: datetime) -> float:
    start = datetime(t.year, 1, 1)
    end = datetime(t.year + 1, 1, 1)
    return t.year + (t - start).total_seconds() / (end - start).total_seconds()


def _doy(t: datetime) -> float:
    return float(t.timetuple().tm_yday)


def _design_matrix(ts: list[datetime], n_harmonics: int,
                   year_centre: Optional[float] = None
                   ) -> tuple[np.ndarray, float, list[str]]:
    years = np.array([_decimal_year(t) for t in ts], dtype=np.float64)
    doys = np.array([_doy(t) for t in ts], dtype=np.float64)
    yc = float(year_centre) if year_centre is not None else float(years.mean())
    cols = [np.ones_like(years), years - yc]
    names = ["intercept", "year"]
    for k in range(1, n_harmonics + 1):
        ang = 2.0 * np.pi * k * doys / 365.25
        cols.append(np.sin(ang)); names.append(f"sin{k}")
        cols.append(np.cos(ang)); names.append(f"cos{k}")
    return np.stack(cols, axis=1), yc, names


def _ridge_solve(X: np.ndarray, Y: np.ndarray,
                 alpha: float, weights: Optional[np.ndarray] = None
                 ) -> np.ndarray:
    """Vectorised ridge regression.  X: (T, p), Y: (T, n), returns (p, n).

    Equivalent to (X^T W X + alpha*I) β = X^T W Y, solved per-column simultaneously.
    Ridge shrinkage on intercept is suppressed (set to 0) so the average of Y is
    preserved. Without weights this is plain ridge; with weights = bool mask we
    do a weighted ridge (NaN rows contribute 0 to both sides).
    """
    p = X.shape[1]
    if weights is None:
        XtX = X.T @ X
        XtY = X.T @ Y
    else:
        Xw = X * weights[:, None]
        XtX = Xw.T @ X
        XtY = Xw.T @ Y
    reg = alpha * np.eye(p)
    reg[0, 0] = 0.0     # don't penalise the intercept
    return np.linalg.solve(XtX + reg, XtY)


def fit(ts: list[datetime], arr: np.ndarray, *,
        n_harmonics: int = 2,
        min_observations: int = 4,
        ridge_alpha: float = 0.5) -> TemporalRegressionFit:
    """Fit one ridge-regression model per pixel.

    `ridge_alpha` shrinks coefficients toward zero, stabilising fits when the
    number of samples is comparable to the number of features (e.g. <10 Landsat
    scenes per pixel). The intercept is not penalised, so the per-pixel mean
    is recovered exactly when alpha is large.

    Pixels with fewer than `min_observations` non-NaN samples drop the year
    trend; pixels with no valid samples get the grid-wide seasonal climatology.
    """
    if arr.ndim != 3:
        raise ValueError(f"expected (T, H, W); got {arr.shape}")
    T, H, W = arr.shape
    X, yc, names = _design_matrix(list(ts), n_harmonics)
    n_feat = X.shape[1]
    Y = arr.reshape(T, H * W).astype(np.float64)

    valid = np.isfinite(Y)
    n_valid = valid.sum(axis=0)

    # build a "cleaned" Y matrix by zero-filling NaN. With weights = valid-mask,
    # the zero rows do not contribute to the ridge normal equations.
    Y_clean = np.where(valid, Y, 0.0)
    coef = np.zeros((n_feat, H * W), dtype=np.float32)

    enough = n_valid >= min_observations
    if enough.any():
        sub_idx = np.where(enough)[0]
        # solve per-pixel, but vectorise over columns by sharing X. Ridge
        # is identical across columns so we can use weighted ridge with one
        # call per pixel only if weights differ; here they may differ per
        # pixel so we batch by unique-mask groups for speed.
        masks_packed = valid[:, sub_idx].astype(np.uint8)
        # pack columns by their mask pattern
        keys = masks_packed.tobytes()  # not directly hashable by column
        # fall back: per-column ridge is fast since solve is small (p~6)
        Y_sub = Y_clean[:, sub_idx]
        m_sub = valid[:, sub_idx]
        coef_sub = np.empty((n_feat, sub_idx.size), dtype=np.float32)
        # group columns by mask hash for batched solve
        # (typical case: most pixels share the same scene-availability)
        seen: dict[bytes, list[int]] = {}
        for j in range(sub_idx.size):
            k = m_sub[:, j].tobytes()
            seen.setdefault(k, []).append(j)
        for mask_bytes, cols in seen.items():
            m = np.frombuffer(mask_bytes, dtype=bool)
            cols_arr = np.array(cols, dtype=np.int64)
            coef_sub[:, cols_arr] = _ridge_solve(
                X[m], Y_sub[m][:, cols_arr], ridge_alpha).astype(np.float32)
        coef[:, sub_idx] = coef_sub

    sparse = (n_valid > 0) & (~enough)
    if sparse.any():
        sub_idx = np.where(sparse)[0]
        X_clim = X.copy(); X_clim[:, 1] = 0.0   # drop year column
        Y_sub = Y_clean[:, sub_idx]
        m_sub = valid[:, sub_idx]
        coef_sub = np.empty((n_feat, sub_idx.size), dtype=np.float32)
        for j in range(sub_idx.size):
            m = m_sub[:, j]
            if m.sum() == 0:
                continue
            coef_sub[:, j] = _ridge_solve(
                X_clim[m], Y_sub[m, j:j+1], ridge_alpha).ravel().astype(np.float32)
        coef[:, sub_idx] = coef_sub

    none = n_valid == 0
    if none.any():
        # use the grid-wide finite mean per timestep as a representative pixel,
        # then write its climatology coefficients to all empty pixels
        with np.errstate(invalid="ignore"):
            Y_avg = np.nanmean(Y, axis=1, keepdims=True)
        if not np.all(np.isfinite(Y_avg)):
            Y_avg = np.where(np.isfinite(Y_avg), Y_avg,
                             np.nanmean(Y_avg))
            Y_avg = np.nan_to_num(Y_avg, nan=0.0)
        X_clim = X.copy(); X_clim[:, 1] = 0.0
        B = _ridge_solve(X_clim, Y_avg, ridge_alpha)
        coef[:, np.where(none)[0]] = np.broadcast_to(
            B.astype(np.float32), (n_feat, int(none.sum())))

    return TemporalRegressionFit(
        coef=coef.reshape(n_feat, H, W),
        n_harmonics=n_harmonics,
        year_centre=yc,
        feature_names=names,
    )


def predict(fit_obj: TemporalRegressionFit,
            ts: list[datetime]) -> np.ndarray:
    """Return (T_target, H, W) predictions for the given timestamps."""
    X, _, _ = _design_matrix(
        list(ts), fit_obj.n_harmonics, year_centre=fit_obj.year_centre)
    H, W = fit_obj.coef.shape[1], fit_obj.coef.shape[2]
    coef_flat = fit_obj.coef.reshape(fit_obj.n_features, -1)
    pred = X @ coef_flat
    return pred.reshape(-1, H, W).astype(np.float32)


def fit_and_predict(ts_train: list[datetime], arr: np.ndarray,
                    ts_target: list[datetime], *,
                    n_harmonics: int = 2,
                    min_observations: int = 4,
                    ridge_alpha: float = 0.5,
                    clip: Optional[tuple[float, float]] = None) -> np.ndarray:
    fit_obj = fit(ts_train, arr, n_harmonics=n_harmonics,
                  min_observations=min_observations,
                  ridge_alpha=ridge_alpha)
    pred = predict(fit_obj, ts_target)
    if clip is not None:
        pred = np.clip(pred, clip[0], clip[1])
    return pred
