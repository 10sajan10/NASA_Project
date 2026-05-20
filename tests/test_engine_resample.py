"""engine.resample: spatial + temporal resampling utilities.

Model-agnostic, no cube / no producer / no model touched. Pure numpy +
datetime in / numpy out.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.resample import resample_spatial, resample_temporal


# ============================================================ spatial
class TestResampleSpatial:
    def test_identity_when_pixel_sizes_match(self):
        arr = np.arange(36, dtype="float32").reshape(6, 6)
        out = resample_spatial(arr, src_pixel_m=30.0, dst_pixel_m=30.0)
        assert out is arr or np.array_equal(out, arr)

    def test_upsample_doubles_shape_bilinear(self):
        arr = np.arange(16, dtype="float32").reshape(4, 4)
        out = resample_spatial(arr, src_pixel_m=20.0, dst_pixel_m=10.0,
                                method="bilinear")
        # ratio = 2 -> 4*2 = 8 (scipy.ndimage.zoom convention)
        assert out.shape == (8, 8)
        assert out.dtype == arr.dtype

    def test_downsample_halves_shape(self):
        arr = np.ones((8, 8), dtype="float32")
        out = resample_spatial(arr, src_pixel_m=10.0, dst_pixel_m=20.0,
                                method="bilinear")
        assert out.shape == (4, 4)
        assert np.allclose(out, 1.0)

    def test_block_mean_downsample_integer_ratio(self):
        # 4x4 block-averaged 2x2 -> mean of each 2x2 block
        arr = np.array([
            [1, 1, 2, 2],
            [1, 1, 2, 2],
            [3, 3, 4, 4],
            [3, 3, 4, 4],
        ], dtype="float32")
        out = resample_spatial(arr, src_pixel_m=10.0, dst_pixel_m=20.0,
                                method="mean")
        assert out.shape == (2, 2)
        assert np.allclose(out, [[1, 2], [3, 4]])

    def test_nearest_preserves_integer_labels(self):
        # Categorical map (fuel classes etc.) — nearest must not blend.
        arr = np.array([[1, 2], [3, 4]], dtype="int32")
        out = resample_spatial(arr.astype("float32"),
                                src_pixel_m=20.0, dst_pixel_m=10.0,
                                method="nearest")
        assert out.shape == (4, 4)
        unique = set(np.unique(out).tolist())
        assert unique <= {1.0, 2.0, 3.0, 4.0}

    def test_three_d_array_is_handled(self):
        arr = np.ones((3, 4, 4), dtype="float32")
        out = resample_spatial(arr, src_pixel_m=20.0, dst_pixel_m=10.0)
        assert out.shape == (3, 8, 8)

    def test_invalid_pixel_size_raises(self):
        with pytest.raises(ValueError, match="positive"):
            resample_spatial(np.zeros((2, 2)),
                              src_pixel_m=0.0, dst_pixel_m=10.0)

    def test_invalid_ndim_raises(self):
        with pytest.raises(ValueError, match="ndim"):
            resample_spatial(np.zeros(10), src_pixel_m=1.0, dst_pixel_m=2.0)


# ============================================================ temporal
class TestResampleTemporal:
    def _src(self, hours=4):
        t0 = datetime(2026, 1, 1)
        ts = [t0 + timedelta(hours=i) for i in range(hours)]
        arr = np.array([[[float(i)]] for i in range(hours)], dtype="float32")
        return ts, arr  # shape (T, 1, 1) with value == t-step

    def test_linear_interp_midpoint(self):
        src_t, src = self._src(hours=4)
        # midpoint between t=1 and t=2 -> value 1.5
        mid = src_t[1] + timedelta(minutes=30)
        out = resample_temporal(src_t, src, [mid], method="linear")
        assert out.shape == (1, 1, 1)
        assert out[0, 0, 0] == pytest.approx(1.5)

    def test_nearest_picks_closest(self):
        src_t, src = self._src(hours=4)
        slightly_after_t2 = src_t[2] + timedelta(minutes=5)
        out = resample_temporal(src_t, src, [slightly_after_t2],
                                 method="nearest")
        assert out[0, 0, 0] == pytest.approx(2.0)

    def test_extrapolation_pads_with_endpoint(self):
        # np.interp clamps outside the range — verify that behaviour.
        src_t, src = self._src(hours=3)
        before = src_t[0] - timedelta(hours=1)
        after = src_t[-1] + timedelta(hours=10)
        out = resample_temporal(src_t, src, [before, after], method="linear")
        assert out[0, 0, 0] == pytest.approx(0.0)
        assert out[1, 0, 0] == pytest.approx(2.0)

    def test_shape_mismatch_raises(self):
        with pytest.raises(ValueError, match="time steps"):
            resample_temporal(
                [datetime(2026, 1, 1)],
                np.zeros((2, 1, 1), dtype="float32"),
                [datetime(2026, 1, 1)])

    def test_unsorted_src_times_are_handled(self):
        # Build out-of-order times and confirm interp result is the same.
        t0 = datetime(2026, 1, 1)
        ordered = [t0 + timedelta(hours=i) for i in range(4)]
        shuffled = [ordered[2], ordered[0], ordered[3], ordered[1]]
        shuffled_arr = np.array([[[2.0]], [[0.0]], [[3.0]], [[1.0]]],
                                 dtype="float32")
        target = [ordered[1] + timedelta(minutes=30)]
        out = resample_temporal(shuffled, shuffled_arr, target,
                                 method="linear")
        # Should still be 1.5 — same as the ordered case.
        assert out[0, 0, 0] == pytest.approx(1.5)

    def test_unknown_method_raises(self):
        src_t, src = self._src(hours=2)
        with pytest.raises(ValueError, match="unknown method"):
            resample_temporal(src_t, src, src_t, method="cubic")
