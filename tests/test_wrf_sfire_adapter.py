"""WRF-SFIRE plug-in tests.

Verifies the full data flow without requiring the actual WRF binary:
  * Template namelist is copied and patched (fire_tign_in_time set,
    grid dims rewritten, sim seconds applied, etc.)
  * wrfinput_d01.nc gets TIGN_IN / NFUEL_CAT / ZSF on the fire mesh
    derived from cube vars (with the right upsample ratio)
  * A fake "wrf.exe" subprocess runs over the staged inputs and writes
    a synthetic wrfout that the parser reads back into the cube
  * The whole thing runs through the engine PipelineRunner just like
    any other ModelAdapter — proving WRF-SFIRE is now a plug-in
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import (
    Pipeline,
    PipelineRunner,
    SerialBackend,
    to_engine_registry,
)
from models.wrf_sfire_adapter import (
    WRFSFireAdapter,
    _UNIGNITED_SENTINEL_S,
    _block_reduce,
    _block_replicate,
    _patch_namelist_value,
)


# ========================================================== unit utilities
def test_patch_namelist_swaps_existing_key():
    nl = " fire_tign_in_time = 100.0,  ! comment kept"
    out = _patch_namelist_value(nl, "fire_tign_in_time", "37.5")
    assert "37.5" in out
    assert "100.0" not in out
    assert "! comment kept" in out


def test_patch_namelist_patches_all_occurrences():
    """A well-formed namelist defines each key at most once per section,
    so 'patch all' and 'patch first' coincide. Documenting the contract."""
    nl = "dx = 50,\ndx = 999,"
    out = _patch_namelist_value(nl, "dx", "100")
    assert "dx = 100," in out
    assert "999" not in out


def test_block_replicate_upsamples_4x():
    a = np.arange(4).reshape(2, 2).astype("float32")
    b = _block_replicate(a, (4, 4))
    assert b.shape == (8, 8)
    # corner (0..4, 0..4) all equal to a[0,0]
    assert np.all(b[:4, :4] == a[0, 0])
    assert np.all(b[4:, 4:] == a[1, 1])


def test_block_reduce_min_picks_smallest_in_block():
    a = np.arange(16).reshape(4, 4).astype("float32")
    out = _block_reduce(a, (2, 2), op="min")
    # top-left block (0,1,4,5) -> min 0; top-right (2,3,6,7) -> 2;
    # bottom-left (8,9,12,13) -> 8; bottom-right (10,11,14,15) -> 10
    assert out.shape == (2, 2)
    assert np.array_equal(out, np.array([[0, 2], [8, 10]], dtype="float32"))


# ========================================================== fake binaries
_FAKE_WRF_SCRIPT = '''\
#!/usr/bin/env python3
"""Stand-in for wrf.exe used in tests.

Reads TIGN_IN from wrfinput_d01.nc and emits a synthetic
wrfout_d01_0001-01-01_00:00:00 NetCDF with:
  * TIGN_G = TIGN_IN clamped to the simulation window (so pre-ignited
    cells keep their times; un-ignited cells keep the SFIRE sentinel)
  * FIRE_AREA = 1.0 where TIGN_G < sentinel, else 0.0

A real wrf.exe would actually propagate fire from the pre-ignited cells.
For tests we only need to validate the round-trip of the data shape.
"""
import sys
from pathlib import Path
import numpy as np
import netCDF4 as nc

stage = Path.cwd()
with nc.Dataset(stage / "wrfinput_d01.nc") as src:
    tign_in = np.asarray(src.variables["TIGN_IN"][:])
    Hf, Wf = tign_in.shape

# Build a (Time, sn_sub, we_sub) frame mirroring real WRF output.
out_path = stage / "wrfout_d01_0001-01-01_00:00:00"
with nc.Dataset(out_path, "w", format="NETCDF4") as ds:
    ds.createDimension("Time", 1)
    ds.createDimension("south_north_subgrid", Hf)
    ds.createDimension("west_east_subgrid",   Wf)
    tg = ds.createVariable("TIGN_G", "f4",
                            ("Time", "south_north_subgrid",
                             "west_east_subgrid"))
    fa = ds.createVariable("FIRE_AREA", "f4",
                            ("Time", "south_north_subgrid",
                             "west_east_subgrid"))
    tg[0] = tign_in
    fa[0] = np.where(tign_in < 1e8, 1.0, 0.0).astype("f4")
'''


@pytest.fixture
def fake_wrf_binary(tmp_path: Path) -> Path:
    p = tmp_path / "fake_wrf.py"
    p.write_text(_FAKE_WRF_SCRIPT)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


@pytest.fixture
def sfire_templates(tmp_path: Path) -> Path:
    """A minimal sfire_dir holding just the templates the producer
    reads. We don't ship actual SFIRE templates here — synthesize a
    Fortran-style namelist with every key the producer patches, so the
    namelist patcher exercises real string substitution."""
    sfire_dir = tmp_path / "sfire_templates"
    sfire_dir.mkdir()
    (sfire_dir / "namelist.input_ignite_from_tign_in").write_text(
        "&time_control\n"
        " run_days    = 0,\n"
        " run_hours   = 0,\n"
        " run_minutes = 1,\n"
        " run_seconds = 0,\n"
        " end_hour    = 1,\n"
        " end_minute  = 0,\n"
        " end_second  = 0,\n"
        " history_interval_s = 60,\n"
        "/\n"
        "&domains\n"
        " e_we = 43,\n"
        " e_sn = 43,\n"
        " dx = 50,\n"
        " dy = 50,\n"
        " sr_x = 10,\n"
        " sr_y = 10,\n"
        "/\n"
        "&fire\n"
        " fire_tign_in_time = 1.0,\n"
        " fire_num_ignitions = 0,\n"
        "/\n"
    )
    (sfire_dir / "namelist.fire").write_text("&fuel_scalars\n/\n")
    (sfire_dir / "input_sounding").write_text("# minimal sounding\n")
    return sfire_dir


# ========================================================== cube fixture
def _cube_with_asteroid(tmp_path: Path):
    """Build a real cube + seed it with the variables WRF-SFIRE consumes."""
    from cube.grid import SimulationGrid
    from cube.store import Cube
    grid = SimulationGrid.from_center_radius(
        -96.797, 32.776, 2_500.0, 500.0)        # 11 x 11 cells
    cube = Cube(tmp_path, grid)
    H, W = grid.shape

    # Asteroid pulse: cells within a 4-cell radius of center get an
    # ignition time scaled by distance from the center.
    cy, cx = H // 2, W // 2
    y, x = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    r = np.sqrt((y - cy) ** 2 + (x - cx) ** 2)
    ign = np.where(r <= 4, 1.0 + r * 0.5, np.nan).astype("float32")
    cube.write_static("ignition_t0", ign,
                       source="thermal_driver", native_res_m=500.0,
                       producer="thermal", units="s")
    cube.write_static("nfuel_cat",
                       np.full((H, W), 3, dtype="float32"),
                       source="landfire_fbfm13", native_res_m=30.0,
                       producer="landfire")
    cube.write_static("dem",
                       np.full((H, W), 300.0, dtype="float32"),
                       source="dem_driver", native_res_m=30.0,
                       producer="dem", units="m")
    cube.write_static("burnable",
                       np.ones((H, W), dtype="float32"),
                       source="thermal_driver", native_res_m=500.0,
                       producer="thermal")
    return cube, ign


# ========================================================== producer
def _build_producer(sfire_templates: Path, fake_wrf: Path,
                    fire_mesh_ratio: int = 2,
                    sim_seconds: int = 600):
    return WRFSFireAdapter(
        sfire_dir=sfire_templates,
        ideal_cmd=None,                                  # test mode
        wrf_cmd=[sys.executable, str(fake_wrf)],
        sim_seconds=sim_seconds,
        history_interval_s=60,
        fire_mesh_ratio=fire_mesh_ratio,
    )


# ========================================================== integration
def test_namelist_gets_fire_tign_in_time_from_max_ignition(
        tmp_path, sfire_templates, fake_wrf_binary):
    """Producer must set fire_tign_in_time > max(ignition_t0) so SFIRE
    accepts all pre-ignited cells."""
    cube, ign = _cube_with_asteroid(tmp_path / "cube")
    model = _build_producer(sfire_templates, fake_wrf_binary)
    reg = to_engine_registry([model])
    pipe = Pipeline.from_targets(["arrival_s", "fire_area"], registry=reg)
    runner = PipelineRunner(reg, backend=SerialBackend(),
                             verbose=False)
    # Keep the stage so we can inspect the namelist after the run.
    res = runner.run(cube, pipe,
                      context={"keep_stage": True})
    assert res.ok, [s.error for s in res.steps if s.status == "error"]

    # Find the stage dir (model wrote it to a tempdir).
    # Easiest path: search /tmp for "wrf_sfire_asteroid_*".
    import tempfile
    candidates = sorted(Path(tempfile.gettempdir()).glob(
        "wrf_sfire_asteroid_*/namelist.input"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    assert candidates, "stage dir was not preserved with keep_stage=True"
    nml = candidates[0].read_text()
    # max ignition_t0 = 1.0 + 4 * 0.5 = 3.0; fire_tign_in_time = 4.0
    assert "fire_tign_in_time = 4.000" in nml
    # Grid dims rewritten from cube grid shape (H, W) = (11, 11)
    assert "e_we = 12" in nml      # WRF e_we = nx + 1 = 11 + 1 = 12
    assert "e_sn = 12" in nml
    assert "dx = 500.0" in nml
    assert "sr_x = 2" in nml       # fire_mesh_ratio default in our test
    cube.close()


def test_wrfinput_carries_asteroid_tign_pattern(
        tmp_path, sfire_templates, fake_wrf_binary):
    """Open the patched wrfinput_d01.nc and confirm TIGN_IN encodes the
    asteroid pattern at the right fire-mesh resolution."""
    cube, ign = _cube_with_asteroid(tmp_path / "cube")
    fmr = 2
    model = _build_producer(sfire_templates, fake_wrf_binary,
                             fire_mesh_ratio=fmr)
    reg = to_engine_registry([model])
    pipe = Pipeline.from_targets(["arrival_s"], registry=reg)
    runner = PipelineRunner(reg, backend=SerialBackend(),
                             verbose=False)
    runner.run(cube, pipe, context={"keep_stage": True})

    import netCDF4 as nc
    import tempfile
    candidates = sorted(Path(tempfile.gettempdir()).glob(
        "wrf_sfire_asteroid_*/wrfinput_d01.nc"),
        key=lambda p: p.stat().st_mtime, reverse=True)
    assert candidates
    with nc.Dataset(candidates[0]) as ds:
        tign_in = np.asarray(ds.variables["TIGN_IN"][:])
        nfuel = np.asarray(ds.variables["NFUEL_CAT"][:])
        zsf = np.asarray(ds.variables["ZSF"][:])
    H, W = cube.grid.shape
    assert tign_in.shape == (H * fmr, W * fmr)
    # Cells that did NOT ignite from the asteroid got the sentinel.
    sentinel_mask = tign_in > 0.99 * _UNIGNITED_SENTINEL_S
    # In the asteroid pattern only cells within r<=4 ignited;
    # everything else is sentinel.
    expected_unignited = np.isnan(ign)
    expected_unignited_fire = _block_replicate(
        expected_unignited.astype("float32"), (fmr, fmr)).astype(bool)
    assert np.array_equal(sentinel_mask, expected_unignited_fire)
    # Fuel cat replicated at fire mesh = 3 everywhere
    assert np.all(nfuel == 3.0)
    # DEM replicated = 300 everywhere
    assert np.all(zsf == 300.0)
    cube.close()


def test_arrival_s_lands_back_in_cube(
        tmp_path, sfire_templates, fake_wrf_binary):
    """End-to-end: producer runs, fake wrf emits wrfout, parser reads
    TIGN_G + FIRE_AREA, ProducerV2.update writes them to the cube,
    cube serves them on read."""
    cube, ign = _cube_with_asteroid(tmp_path / "cube")
    model = _build_producer(sfire_templates, fake_wrf_binary,
                             fire_mesh_ratio=2)
    reg = to_engine_registry([model])
    pipe = Pipeline.from_targets(["arrival_s", "fire_area"], registry=reg)
    runner = PipelineRunner(reg, backend=SerialBackend(),
                             verbose=False)
    res = runner.run(cube, pipe)
    assert res.ok

    arrival = cube.read_static("arrival_s")
    fire_area = cube.read_static("fire_area")
    # Same shape as the cube grid (downsampled from fire mesh)
    assert arrival.shape == cube.grid.shape
    assert fire_area.shape == cube.grid.shape

    # Cells inside the asteroid footprint -> finite arrival time;
    # outside -> NaN (sentinel mapped to NaN by parse_outputs).
    inside = ~np.isnan(ign)
    outside = np.isnan(ign)
    assert np.all(np.isfinite(arrival[inside]))
    assert np.all(np.isnan(arrival[outside]))
    # FIRE_AREA = 1 inside, 0 outside (per the fake binary's rule)
    assert np.allclose(fire_area[inside], 1.0)
    assert np.allclose(fire_area[outside], 0.0)
    cube.close()


def test_second_run_is_skipped(
        tmp_path, sfire_templates, fake_wrf_binary):
    """The WRF binary is expensive — a second run on the same cube must
    be skipped via cube.satisfies, just like any other producer."""
    cube, _ = _cube_with_asteroid(tmp_path / "cube")
    model = _build_producer(sfire_templates, fake_wrf_binary,
                             fire_mesh_ratio=2)
    reg = to_engine_registry([model])
    pipe = Pipeline.from_targets(["arrival_s", "fire_area"], registry=reg)
    runner = PipelineRunner(reg, backend=SerialBackend(),
                             verbose=False)
    runner.run(cube, pipe)
    res2 = runner.run(cube, pipe)
    assert res2.by_name()["wrf_sfire_asteroid"].status == "skipped"
    cube.close()


def test_dirty_propagation_when_asteroid_field_bumped(
        tmp_path, sfire_templates, fake_wrf_binary):
    """Re-fetch ignition_t0 -> SFIRE output is stale -> producer re-runs.
    This is the cross-cutting property: bumping any upstream input
    cascades through the model adapter just like every other producer."""
    import time
    cube, _ = _cube_with_asteroid(tmp_path / "cube")
    model = _build_producer(sfire_templates, fake_wrf_binary,
                             fire_mesh_ratio=2)
    reg = to_engine_registry([model])
    pipe = Pipeline.from_targets(["arrival_s"], registry=reg)
    runner = PipelineRunner(reg, backend=SerialBackend(),
                             verbose=False)
    runner.run(cube, pipe)

    time.sleep(0.05)
    H, W = cube.grid.shape
    cube.write_static(
        "ignition_t0",
        np.full((H, W), 0.5, dtype="float32"),
        source="thermal_v2", native_res_m=500.0,
        producer="thermal", units="s")
    res = runner.run(cube, pipe)
    assert res.by_name()["wrf_sfire_asteroid"].status == "ok", (
        "stale check failed: producer should have re-run after the "
        "upstream ignition_t0 was bumped")
    cube.close()
