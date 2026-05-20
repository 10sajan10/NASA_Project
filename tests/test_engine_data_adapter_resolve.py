"""DataAdapter.resolve + fetch-with-resample + cube.time_coverage.

These tests pin the user's "time-aware variable lookup" contract:

  1. Cube already has data for the requested window      -> via="cube"
  2. Cube doesn't, but a producer is registered          -> via="producer:..."
  3. Cube doesn't, no producer is registered             -> NoDataAvailable
  4. Cube has the data but at a different resolution     -> fetch resamples
     (spatial or temporal), without touching the cube.

Model- and variable-agnostic: the variable names are 'foo' / 'bar' /
'baz_t' — no physics, no fire, no specific producer baked in.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    DataAdapter,
    DataNeed,
    NoDataAvailable,
    ProducerRegistry,
    Request,
)


# ============================================================ helpers
def _real_cube(tmp_path: Path, pixel_m: float = 500.0):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 2_500.0, pixel_m)
    return Cube(tmp_path, grid)


class _FakeProducer:
    """Minimal producer-shaped object: writes a fixed array into the cube.
    Used to exercise the registry / resolve path without any model code."""

    def __init__(self, name: str, variable: str,
                 kind: str = "static",
                 value: float = 7.0,
                 native_res_m: float = 100.0,
                 ts_hours: int = 0):
        self.name = name
        self.produces = (variable,)
        self.requires = ()
        self._variable = variable
        self._kind = kind
        self._value = value
        self._native_res_m = native_res_m
        self._ts_hours = ts_hours
        self.call_count = 0

    def run(self, cube, request) -> dict[str, int]:
        self.call_count += 1
        H, W = cube.grid.shape
        if self._kind == "static":
            cube.write_static(
                self._variable,
                np.full((H, W), self._value, dtype="float32"),
                source=f"producer:{self.name}",
                native_res_m=self._native_res_m,
                units="", producer=self.name)
            return {self._variable: 1}
        # time
        t0 = getattr(request, "t_start", None) or datetime(2026, 1, 1)
        ts = [t0 + timedelta(hours=i) for i in range(self._ts_hours)]
        arr = np.full((len(ts), H, W), self._value, dtype="float32")
        cube.write_3d(
            self._variable, ts, arr,
            source=f"producer:{self.name}",
            native_res_m=self._native_res_m,
            units="", producer=self.name)
        return {self._variable: 1}


# ============================================================ time_coverage
class TestCubeTimeCoverage:
    def test_reports_window_covered_when_tiles_present(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            H, W = cube.grid.shape
            t0 = datetime(2026, 5, 1)
            ts = [t0 + timedelta(hours=i) for i in range(6)]
            arr = np.ones((6, H, W), dtype="float32")
            cube.write_3d("wind", ts, arr,
                           source="t", native_res_m=10_000.0)
            rep = cube.time_coverage("wind", t0, t0 + timedelta(hours=5))
            assert rep["window_covered"] is True
            assert len(rep["times_in_range"]) == 6
            assert rep["first"] == ts[0]
            assert rep["last"] == ts[-1]
        finally:
            cube.close()

    def test_reports_empty_when_variable_absent(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            rep = cube.time_coverage(
                "missing", datetime(2026, 1, 1),
                datetime(2026, 1, 2))
            assert rep["window_covered"] is False
            assert rep["times_in_range"] == []
            assert rep["first"] is None
            assert rep["last"] is None
        finally:
            cube.close()

    def test_window_outside_existing_tiles_is_not_covered(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            H, W = cube.grid.shape
            t0 = datetime(2026, 5, 1)
            cube.write_3d("v", [t0], np.zeros((1, H, W), dtype="float32"),
                           source="t", native_res_m=10_000.0)
            rep = cube.time_coverage(
                "v",
                t0 + timedelta(days=10),
                t0 + timedelta(days=11))
            assert rep["window_covered"] is False
            assert rep["times_in_range"] == []
            # ...but the full-range bookends are still reported.
            assert rep["first"] == t0
        finally:
            cube.close()


# ============================================================ resolve
class TestResolveCubeFirst:
    def test_cube_hit_skips_producer(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            H, W = cube.grid.shape
            cube.write_static(
                "foo", np.ones((H, W), dtype="float32"),
                source="t", native_res_m=100.0)
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            reg = ProducerRegistry()
            prod = _FakeProducer("foo_producer", "foo")
            reg.register(prod)

            report = adapter.resolve(cube, reg, Request())
            assert report["foo"]["satisfied"] is True
            assert report["foo"]["via"] == "cube"
            assert prod.call_count == 0
        finally:
            cube.close()


class TestResolveProducerFallback:
    def test_producer_runs_when_cube_is_empty_static(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            reg = ProducerRegistry()
            prod = _FakeProducer("foo_producer", "foo")
            reg.register(prod)

            report = adapter.resolve(cube, reg, Request())
            assert report["foo"]["satisfied"] is True
            assert report["foo"]["via"] == "producer:foo_producer"
            assert prod.call_count == 1
            assert cube.has("foo")
        finally:
            cube.close()

    def test_producer_runs_when_cube_is_empty_time(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([DataNeed("baz_t", kind="time")])
            reg = ProducerRegistry()
            prod = _FakeProducer("t_prod", "baz_t",
                                  kind="time", ts_hours=3)
            reg.register(prod)
            t0 = datetime(2026, 6, 1)
            report = adapter.resolve(
                cube, reg,
                Request(t_start=t0, t_end=t0 + timedelta(hours=2)))
            assert report["baz_t"]["satisfied"] is True
            assert report["baz_t"]["via"] == "producer:t_prod"
        finally:
            cube.close()

    def test_producer_resolution_below_cap_marks_not_satisfied(self, tmp_path):
        """Producer writes at 1000m, need declares max 100m -> producer
        runs but cube.satisfies still returns False; required need -> raise."""
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([
                DataNeed("foo", kind="static", max_native_res_m=100.0)])
            reg = ProducerRegistry()
            prod = _FakeProducer("coarse_prod", "foo",
                                  native_res_m=1000.0)
            reg.register(prod)

            with pytest.raises(NoDataAvailable, match="foo"):
                adapter.resolve(cube, reg, Request())
            assert prod.call_count == 1  # producer DID run
        finally:
            cube.close()


class TestResolveNoData:
    def test_required_need_without_producer_raises(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            reg = ProducerRegistry()
            with pytest.raises(NoDataAvailable) as exc_info:
                adapter.resolve(cube, reg, Request())
            assert exc_info.value.variable == "foo"
        finally:
            cube.close()

    def test_optional_need_without_producer_is_skipped(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([
                DataNeed("foo", kind="static", required=False)])
            reg = ProducerRegistry()
            report = adapter.resolve(cube, reg, Request())
            assert report["foo"]["satisfied"] is False
            assert report["foo"]["error"] == "no_producer"
        finally:
            cube.close()

    def test_message_carries_variable_and_window(self):
        err = NoDataAvailable(
            "wind_speed_ms",
            datetime(2026, 5, 1), datetime(2026, 5, 2),
            reason="no producer")
        assert "wind_speed_ms" in str(err)
        assert "2026-05-01" in str(err)
        assert "no producer" in str(err)


class TestResolveDuckTypedProducer:
    def test_driver_shaped_fetch_signature_works(self, tmp_path):
        """A producer exposing only `fetch(cube, t_start, t_end)` (no
        `run`) is acceptable: this is the Driver protocol."""
        cube = _real_cube(tmp_path)

        class _FetchDriver:
            name = "drv"
            produces = ("foo",)
            requires = ()

            def __init__(self):
                self.call_count = 0

            def fetch(self, cube, t_start=None, t_end=None):
                self.call_count += 1
                H, W = cube.grid.shape
                cube.write_static(
                    "foo", np.ones((H, W), dtype="float32"),
                    source="drv", native_res_m=100.0)
                return ["foo"]

        try:
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            reg = ProducerRegistry()
            drv = _FetchDriver()
            reg.register(drv)

            report = adapter.resolve(cube, reg, Request())
            assert report["foo"]["satisfied"] is True
            assert drv.call_count == 1
        finally:
            cube.close()


# ============================================================ fetch + resample
class TestFetchWithResample:
    def test_static_target_pixel_resamples(self, tmp_path):
        cube = _real_cube(tmp_path, pixel_m=500.0)
        try:
            H, W = cube.grid.shape
            arr = np.arange(H * W, dtype="float32").reshape(H, W)
            # Catalog-native is 500m (matches cube).
            cube.write_static(
                "foo", arr,
                source="t", native_res_m=500.0)
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            out = adapter.fetch(cube, target_pixel_m=250.0)
            new_h = out["foo"].shape[0]
            new_w = out["foo"].shape[1]
            assert new_h > H and new_w > W
        finally:
            cube.close()

    def test_static_target_pixel_equal_to_native_is_no_op(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            H, W = cube.grid.shape
            cube.write_static(
                "foo", np.ones((H, W), dtype="float32"),
                source="t", native_res_m=500.0)
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            out = adapter.fetch(cube, target_pixel_m=500.0)
            assert out["foo"].shape == (H, W)
        finally:
            cube.close()

    def test_time_target_times_resamples_to_new_axis(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            H, W = cube.grid.shape
            t0 = datetime(2026, 5, 1)
            src_t = [t0 + timedelta(hours=i) for i in range(4)]
            src = np.stack([np.full((H, W), float(i), dtype="float32")
                            for i in range(4)])
            cube.write_3d("baz_t", src_t, src,
                           source="t", native_res_m=10_000.0)
            adapter = DataAdapter([DataNeed("baz_t", kind="time")])
            # Interpolate to half-hour offsets.
            target = [t0 + timedelta(minutes=30),
                      t0 + timedelta(hours=1, minutes=30)]
            out_ts, out_arr = adapter.fetch(
                cube, target_times=target)["baz_t"]
            assert out_ts == target
            assert out_arr.shape == (2, H, W)
            # value(t=0.5h) = 0.5; value(t=1.5h) = 1.5
            assert np.allclose(out_arr[0], 0.5)
            assert np.allclose(out_arr[1], 1.5)
        finally:
            cube.close()

    def test_optional_absent_returns_none(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([
                DataNeed("foo", kind="static", required=False)])
            out = adapter.fetch(cube)
            assert out["foo"] is None
        finally:
            cube.close()

    def test_required_absent_raises(self, tmp_path):
        cube = _real_cube(tmp_path)
        try:
            adapter = DataAdapter([DataNeed("foo", kind="static")])
            with pytest.raises(RuntimeError, match="required"):
                adapter.fetch(cube)
        finally:
            cube.close()


# ============================================================ windowed read
class TestRead3DWindow:
    def _seed_cube(self, tmp_path):
        cube = _real_cube(tmp_path)
        H, W = cube.grid.shape
        t0 = datetime(2026, 6, 1)
        ts = [t0 + timedelta(hours=i) for i in range(12)]
        arr = np.stack([np.full((H, W), float(i), dtype="float32")
                        for i in range(12)])
        cube.write_3d("v", ts, arr,
                       source="t", native_res_m=10_000.0)
        return cube, t0

    def test_returns_only_timesteps_inside_window(self, tmp_path):
        cube, t0 = self._seed_cube(tmp_path)
        try:
            ts, arr = cube.read_3d_window(
                "v",
                t0 + timedelta(hours=3),
                t0 + timedelta(hours=6))
            assert len(ts) == 4
            assert arr.shape[0] == 4
            assert np.allclose(arr[:, 0, 0], [3.0, 4.0, 5.0, 6.0])
        finally:
            cube.close()

    def test_open_ended_window_uses_full_extent(self, tmp_path):
        cube, t0 = self._seed_cube(tmp_path)
        try:
            ts_full, _ = cube.read_3d_window("v", None, None)
            assert len(ts_full) == 12
            ts_late, arr_late = cube.read_3d_window(
                "v", t0 + timedelta(hours=10), None)
            assert len(ts_late) == 2
            assert np.allclose(arr_late[:, 0, 0], [10.0, 11.0])
        finally:
            cube.close()

    def test_empty_window_returns_empty_arrays(self, tmp_path):
        cube, t0 = self._seed_cube(tmp_path)
        try:
            ts, arr = cube.read_3d_window(
                "v",
                t0 + timedelta(days=30),
                t0 + timedelta(days=31))
            assert ts == []
            assert arr.shape[0] == 0
        finally:
            cube.close()

    def test_fetch_uses_windowed_read_when_request_has_time(self, tmp_path):
        """DataAdapter.fetch should load only the window when the request
        carries t_start/t_end — verified by checking returned T < full."""
        cube, t0 = self._seed_cube(tmp_path)
        try:
            adapter = DataAdapter([DataNeed("v", kind="time")])
            req = Request(
                t_start=t0 + timedelta(hours=2),
                t_end=t0 + timedelta(hours=4))
            ts, arr = adapter.fetch(cube, req)["v"]
            assert len(ts) == 3
            assert arr.shape[0] == 3
            assert np.allclose(arr[:, 0, 0], [2.0, 3.0, 4.0])
        finally:
            cube.close()
