"""Tests for the WRF scenario config + namelist generator.

Pure-Python (stdlib only) — no numpy / cube / compiled WRF needed.
Validates the fiddly nesting index arithmetic, the per-domain namelist
arrays, the &chem gating, and the single-domain fallback.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.wrf_config import (
    DomainSpec,
    NamelistBuilder,
    WPSNamelistBuilder,
    WRFScenario,
    _build_centered_nests,
)


# ----------------------------------------------------- nest geometry
def test_single_domain_from_extent():
    d = _build_centered_nests(extent_km=1000, resolutions_m=[10000],
                              nest_fraction=0.5)
    assert len(d) == 1
    assert d[0].dx_m == 10000
    assert d[0].nx == 100            # 1000 km / 10 km
    assert d[0].parent_grid_ratio == 1


def test_three_nests_indices_are_wrf_valid():
    d = _build_centered_nests(
        extent_km=1000, resolutions_m=[9000, 3000, 1000],
        nest_fraction=0.5)
    assert len(d) == 3
    # ratios
    assert [x.parent_grid_ratio for x in d] == [1, 3, 3]
    # WRF rule: (e_we - 1) % parent_grid_ratio == 0 for every nest
    for x in d:
        assert (x.e_we - 1) % x.parent_grid_ratio == 0
        assert (x.e_sn - 1) % x.parent_grid_ratio == 0
    # parent_id chains 1 <- 2 <- 3
    assert [x.parent_id for x in d] == [1, 1, 2]
    # nests are centered (start > 1, symmetric in x/y)
    for x in d[1:]:
        assert x.i_parent_start > 1
        assert x.i_parent_start == x.j_parent_start


def test_non_integer_ratio_rejected():
    import pytest
    with pytest.raises(ValueError):
        _build_centered_nests(extent_km=500, resolutions_m=[4000, 1500],
                              nest_fraction=0.5)   # 4000/1500 not integer


# ----------------------------------------------------- scenario
def test_from_simple_puts_fire_mesh_on_innermost():
    sc = WRFScenario.from_simple(
        center_lon=-96.8, center_lat=32.8,
        start=datetime(2019, 9, 4, 12),
        extent_km=1000, resolutions_m=[9000, 3000, 1000],
        fire_mesh_ratio=10)
    assert sc.max_dom == 3
    assert sc.domains[-1].sr == 10
    assert sc.domains[0].sr == 0
    assert sc.fire_domain_index == 3


def test_smoke_sets_chem_opt():
    sc = WRFScenario.from_simple(
        center_lon=-96.8, center_lat=32.8,
        start=datetime(2019, 9, 4, 12),
        resolutions_m=[3000], smoke=True)
    assert sc.smoke
    assert sc.chem_opt != 0       # __post_init__ picks a default
    assert sc.emiss_opt != 0


def test_coarse_time_step_scales_with_dx():
    sc = WRFScenario.from_simple(
        center_lon=0, center_lat=0, start=datetime(2019, 1, 1),
        resolutions_m=[9000])
    assert sc.coarse_time_step() == 54        # ~6 s per km * 9 km


# ----------------------------------------------------- namelist.input
def test_namelist_domains_arrays_match_max_dom():
    sc = WRFScenario.from_simple(
        center_lon=-96.8, center_lat=32.8, start=datetime(2019, 9, 4, 12),
        resolutions_m=[9000, 3000, 1000])
    nml = NamelistBuilder(sc).render()
    assert "max_dom                 = 3," in nml
    # each per-domain row has exactly 3 comma-separated entries
    for key in ("e_we", "dx ", "parent_grid_ratio", "sr_x"):
        line = next(l for l in nml.splitlines() if l.strip().startswith(key))
        n = line.split("=")[1].count(",")
        assert n == 3, f"{key!r} row should have 3 values, got {n}"


def test_namelist_ifire_only_on_fire_domain():
    sc = WRFScenario.from_simple(
        center_lon=-96.8, center_lat=32.8, start=datetime(2019, 9, 4, 12),
        resolutions_m=[9000, 3000, 1000])
    nml = NamelistBuilder(sc).render()
    line = next(l for l in nml.splitlines() if l.strip().startswith("ifire"))
    # fire on innermost only -> 0, 0, 1
    assert line.split("=")[1].strip().rstrip(",").replace(" ", "") == "0,0,1"


def test_chem_section_only_when_smoke():
    base = dict(center_lon=-96.8, center_lat=32.8,
                start=datetime(2019, 9, 4, 12), resolutions_m=[3000])
    assert "&chem" not in NamelistBuilder(
        WRFScenario.from_simple(**base, smoke=False)).render()
    assert "&chem" in NamelistBuilder(
        WRFScenario.from_simple(**base, smoke=True)).render()


def test_namelist_ifire_value_is_1():
    """ifire=1 selects full SFIRE physics (the crash fix)."""
    sc = WRFScenario.from_simple(
        center_lon=0, center_lat=0, start=datetime(2019, 1, 1),
        resolutions_m=[3000])
    nml = NamelistBuilder(sc).render()
    line = next(l for l in nml.splitlines() if l.strip().startswith("ifire"))
    assert "1" in line.split("=")[1]


# ----------------------------------------------------- namelist.wps
def test_wps_namelist_has_matching_max_dom():
    sc = WRFScenario.from_simple(
        center_lon=-96.8, center_lat=32.8, start=datetime(2019, 9, 4, 12),
        resolutions_m=[9000, 3000, 1000])
    wps = WPSNamelistBuilder(sc, geog_data_path="/g").render()
    assert "max_dom = 3," in wps
    assert "ref_lat   = 32.80" in wps
    assert "geog_data_path = '/g'" in wps
