"""Halo I/O tests on the Cube.

Boundary-coupled producers (fire spread, diffusion, convolutional stencils)
need to read a tile padded with a halo of neighbouring cells, compute over
the full halo'd extent, then write back only the inner region. These tests
validate:
  * halo bounds clip cleanly at grid edges (no out-of-bounds reads)
  * read+modify+write_inner round-trip preserves the rest of the cube
  * 3D variants honor the time slab correctly
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ----------------------------------------------------- helpers
def _real_cube(tmp_path: Path, *, radius_m: float = 50_000.0,
               pixel_m: float = 500.0):
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, radius_m, pixel_m)
    return Cube(tmp_path, grid)


def _seed_static(cube, name: str = "field"):
    """Seed a static var where each cell value equals y*W + x (so we can
    verify which cells were read or written)."""
    H, W = cube.grid.shape
    arr = (np.arange(H * W, dtype="float32")).reshape(H, W)
    cube.init_static_tiled(
        name, dtype="float32", source="test",
        native_res_m=float(cube.grid.pixel_m), units="", producer="test",
        chunk=(64, 64), fill_value=np.nan)
    cube.write_chunk_static(name, slice(0, H), slice(0, W), arr)
    return arr


# ----------------------------------------------------- bounds
def test_halo_bounds_clip_at_grid_edges(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        H, W = cube.grid.shape
        # tile sits in the top-left corner; halo of 4 should clip on north/west
        outer_y, outer_x, inner_y, inner_x = cube._halo_bounds(
            slice(0, 32), slice(0, 32), halo=4)
        assert outer_y == slice(0, 36)
        assert outer_x == slice(0, 36)
        # inner slice points to the original tile within the (clipped) outer
        assert inner_y == slice(0, 32)
        assert inner_x == slice(0, 32)

        # interior tile: halo applied on all sides
        outer_y, outer_x, inner_y, inner_x = cube._halo_bounds(
            slice(64, 96), slice(64, 96), halo=4)
        assert outer_y == slice(60, 100)
        assert outer_x == slice(60, 100)
        assert inner_y == slice(4, 36)
        assert inner_x == slice(4, 36)

        # bottom-right tile: clips on south/east only (north/west get
        # full halo). Inner slice starts at offset 4 (halo from north),
        # spans the tile's 32 rows, so inner = (4, 36) within the
        # halo'd array of shape (36, 36).
        outer_y, outer_x, inner_y, inner_x = cube._halo_bounds(
            slice(H - 32, H), slice(W - 32, W), halo=4)
        assert outer_y == slice(H - 36, H)
        assert outer_x == slice(W - 36, W)
        assert inner_y == slice(4, 36)
        assert inner_x == slice(4, 36)
    finally:
        cube.close()


def test_halo_zero_is_identity(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        _seed_static(cube)
        data, inner = cube.read_chunk_static_with_halo(
            "field", slice(32, 64), slice(32, 64), halo=0)
        assert data.shape == (32, 32)
        assert inner == (slice(0, 32), slice(0, 32))
    finally:
        cube.close()


def test_negative_halo_raises(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        with pytest.raises(ValueError):
            cube._halo_bounds(slice(0, 16), slice(0, 16), halo=-1)
    finally:
        cube.close()


# ----------------------------------------------------- static I/O
def test_read_with_halo_returns_neighbour_cells(tmp_path):
    cube = _real_cube(tmp_path)
    try:
        arr = _seed_static(cube)
        H, W = cube.grid.shape
        data, inner = cube.read_chunk_static_with_halo(
            "field", slice(64, 96), slice(64, 96), halo=2)
        # outer covers (62..98, 62..98) -> shape (36, 36)
        assert data.shape == (36, 36)
        # cell (0, 0) of data corresponds to grid cell (62, 62)
        assert float(data[0, 0]) == float(arr[62, 62])
        # inner slice carves out the original tile (rows 2..34, cols 2..34)
        assert inner == (slice(2, 34), slice(2, 34))
        assert np.array_equal(data[inner], arr[64:96, 64:96])
    finally:
        cube.close()


def test_write_inner_preserves_neighbouring_cells(tmp_path):
    """A producer reads with halo, modifies the whole halo'd array, writes
    back only the inner region. Neighbouring tiles (the halo region) must
    NOT get overwritten — that's what write_inner is for."""
    cube = _real_cube(tmp_path)
    try:
        original = _seed_static(cube).copy()
        H, W = cube.grid.shape

        # Read with halo, sentinel the entire halo'd region, write inner.
        data, inner = cube.read_chunk_static_with_halo(
            "field", slice(64, 96), slice(64, 96), halo=4)
        sentinel = np.full_like(data, -777.0)
        cube.write_inner_static(
            "field", slice(64, 96), slice(64, 96),
            sentinel, halo=4)

        # The inner region got the sentinel...
        new = cube.read_static("field")
        assert np.all(new[64:96, 64:96] == -777.0)
        # ...but the halo region (rows 60..64, 96..100) is unchanged
        assert np.array_equal(new[60:64, 60:100], original[60:64, 60:100])
        assert np.array_equal(new[96:100, 60:100], original[96:100, 60:100])
        assert np.array_equal(new[64:96, 60:64], original[64:96, 60:64])
        assert np.array_equal(new[64:96, 96:100], original[64:96, 96:100])
    finally:
        cube.close()


def test_full_halo_round_trip_on_corner_tile(tmp_path):
    """Corner tile has halo clipped to grid bounds. write_inner must still
    write exactly the tile, not a halo-shifted region."""
    cube = _real_cube(tmp_path)
    try:
        original = _seed_static(cube).copy()
        H, W = cube.grid.shape
        data, inner = cube.read_chunk_static_with_halo(
            "field", slice(0, 32), slice(0, 32), halo=4)
        # halo clipped on north/west: outer is (0..36, 0..36), inner is (0..32, 0..32)
        assert data.shape == (36, 36)
        assert inner == (slice(0, 32), slice(0, 32))
        modified = data.copy()
        modified[inner] = -9.0
        cube.write_inner_static(
            "field", slice(0, 32), slice(0, 32), modified, halo=4)
        new = cube.read_static("field")
        assert np.all(new[0:32, 0:32] == -9.0)
        # row 32 (the first row outside the tile) untouched
        assert np.array_equal(new[32, :32], original[32, :32])
    finally:
        cube.close()


# ----------------------------------------------------- 3D variants
def test_time_halo_read_and_inner_write(tmp_path):
    from datetime import datetime, timedelta
    cube = _real_cube(tmp_path, radius_m=10_000.0)
    try:
        H, W = cube.grid.shape
        ts = [datetime(2026, 1, 1) + timedelta(hours=i) for i in range(6)]
        cube.init_time_tiled(
            "u", ts=ts, dtype="float32",
            source="test", native_res_m=float(cube.grid.pixel_m),
            units="", producer="test", chunk=(6, 16, 16))
        # Seed full array: value = t*1000 + y*100 + x
        T = len(ts)
        full = np.empty((T, H, W), dtype="float32")
        for t in range(T):
            for y in range(H):
                full[t, y, :] = t * 1000.0 + y * 100.0 + np.arange(W)
        cube.write_chunk_time("u", slice(0, T), slice(0, H), slice(0, W),
                              full)
        # Read tile (4..12, 4..12) with halo=2 across all time
        data, (full_t, iy, ix) = cube.read_chunk_time_with_halo(
            "u", slice(0, T), slice(4, 12), slice(4, 12), halo=2)
        assert data.shape == (T, 12, 12)
        assert iy == slice(2, 10)
        assert ix == slice(2, 10)
        # Inner region matches the seeded values
        assert np.array_equal(data[:, iy, ix], full[:, 4:12, 4:12])
        # Modify the halo'd array, write_inner only the tile
        modified = data.copy()
        modified[:, iy, ix] = -1.0
        cube.write_inner_time(
            "u", slice(0, T), slice(4, 12), slice(4, 12),
            modified, halo=2)
        after = cube.read_chunk_time("u", slice(0, T),
                                      slice(0, H), slice(0, W))
        # Tile got -1
        assert np.all(after[:, 4:12, 4:12] == -1.0)
        # Halo region around it untouched
        assert np.array_equal(after[:, 2:4, 4:12], full[:, 2:4, 4:12])
        assert np.array_equal(after[:, 12:14, 4:12], full[:, 12:14, 4:12])
    finally:
        cube.close()
