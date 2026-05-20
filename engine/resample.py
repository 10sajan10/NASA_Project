"""Spatial and temporal resampling utilities.

Model- and variable-agnostic. Operates on plain numpy arrays + pixel size
+ time arrays. Used by `DataAdapter.fetch` when a consumer asks for a
different resolution than what the cube currently holds.

Two functions:

  * resample_spatial(arr, src_pixel_m, dst_pixel_m, method)
      Resample (H, W) or (T, H, W) between equal-area grids by pixel size.
      method:
        - "bilinear"  smooth, suitable for continuous fields
        - "nearest"   discrete-label safe (e.g. categorical maps)
        - "mean"      block-average when downsampling integer ratios;
                       falls back to bilinear otherwise

  * resample_temporal(src_times, src_arr, dst_times, method)
      Resample (T, ...) along the time axis to a different set of
      timestamps. method:
        - "linear"   piecewise-linear interpolation (continuous data)
        - "nearest"  pick the closest source timestamp (categorical /
                     instantaneous data)

Both functions are pure: they don't touch the cube, the catalog, or any
specific producer. Callers handle that.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Literal

import numpy as np


SpatialMethod = Literal["bilinear", "nearest", "mean"]
TemporalMethod = Literal["linear", "nearest"]


# ============================================================ spatial
def resample_spatial(arr: np.ndarray,
                      src_pixel_m: float,
                      dst_pixel_m: float,
                      method: SpatialMethod = "bilinear") -> np.ndarray:
    """Resample `arr` from `src_pixel_m` to `dst_pixel_m`.

    Accepts (H, W) or (T, H, W). The output's spatial dims are scaled by
    `src_pixel_m / dst_pixel_m` (>1 upsample, <1 downsample). Dtype is
    preserved.

    For categorical data pass `method="nearest"`. For integer-ratio
    downsampling of continuous data, `method="mean"` gives a true area
    average; non-integer ratios fall back to bilinear.
    """
    if src_pixel_m <= 0 or dst_pixel_m <= 0:
        raise ValueError("pixel size must be positive")
    ratio = float(src_pixel_m) / float(dst_pixel_m)
    if abs(ratio - 1.0) < 1e-9:
        return arr

    if arr.ndim == 2:
        return _resample_2d(arr, ratio, method)
    if arr.ndim == 3:
        out_planes = [_resample_2d(arr[i], ratio, method)
                      for i in range(arr.shape[0])]
        return np.stack(out_planes, axis=0)
    raise ValueError(
        f"resample_spatial: arr.ndim must be 2 or 3, got {arr.ndim}")


def _resample_2d(arr: np.ndarray, ratio: float,
                  method: SpatialMethod) -> np.ndarray:
    # Block-mean fast path for clean integer downsample.
    if method == "mean" and ratio < 1.0:
        inv = 1.0 / ratio
        if abs(inv - round(inv)) < 1e-6:
            return _block_mean_2d(arr, int(round(inv)))
        # non-integer ratio: fall back to bilinear
        method = "bilinear"

    try:
        from scipy.ndimage import zoom
    except Exception as exc:  # pragma: no cover - scipy is a hard dep
        raise RuntimeError(
            "resample_spatial requires scipy.ndimage.zoom") from exc

    order = {"bilinear": 1, "nearest": 0, "mean": 1}[method]
    out = zoom(arr, ratio, order=order)
    return out.astype(arr.dtype, copy=False)


def _block_mean_2d(arr: np.ndarray, factor: int) -> np.ndarray:
    """Average `factor x factor` blocks. Trailing rows/cols that don't
    fill a full block are dropped (deterministic, no padding artefacts)."""
    H, W = arr.shape
    H2 = (H // factor) * factor
    W2 = (W // factor) * factor
    a = arr[:H2, :W2].astype("float64", copy=False)
    a = a.reshape(H2 // factor, factor, W2 // factor, factor)
    return a.mean(axis=(1, 3)).astype(arr.dtype, copy=False)


# ============================================================ temporal
def resample_temporal(src_times: Iterable[datetime],
                       src_arr: np.ndarray,
                       dst_times: Iterable[datetime],
                       method: TemporalMethod = "linear") -> np.ndarray:
    """Resample `src_arr` along axis 0 from `src_times` to `dst_times`.

    `src_arr.shape = (T, ...)`. Returns shape `(len(dst_times),) + ...`.
    Dtype is preserved (float interp is done in float64 internally and
    cast back).
    """
    src_times = list(src_times)
    dst_times = list(dst_times)
    if not src_times:
        raise ValueError("resample_temporal: src_times is empty")
    if src_arr.shape[0] != len(src_times):
        raise ValueError(
            f"resample_temporal: src_arr has {src_arr.shape[0]} time steps "
            f"but src_times has {len(src_times)}")

    src_sec = np.array([t.timestamp() for t in src_times], dtype="float64")
    dst_sec = np.array([t.timestamp() for t in dst_times], dtype="float64")
    # Guard for unsorted input.
    order = np.argsort(src_sec)
    src_sec_sorted = src_sec[order]
    arr_sorted = src_arr[order]

    if method == "nearest":
        idx = np.array(
            [int(np.argmin(np.abs(src_sec_sorted - d))) for d in dst_sec],
            dtype="int64")
        return arr_sorted[idx]

    if method == "linear":
        T = arr_sorted.shape[0]
        flat = arr_sorted.reshape(T, -1).astype("float64", copy=False)
        out_flat = np.empty((len(dst_times), flat.shape[1]),
                            dtype="float64")
        for j in range(flat.shape[1]):
            out_flat[:, j] = np.interp(dst_sec, src_sec_sorted, flat[:, j])
        out = out_flat.reshape((len(dst_times),) + src_arr.shape[1:])
        return out.astype(src_arr.dtype, copy=False)

    raise ValueError(f"resample_temporal: unknown method {method!r}")
