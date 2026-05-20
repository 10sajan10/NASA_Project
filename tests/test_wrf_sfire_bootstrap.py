"""WRF-SFIRE stack bootstrap tests.

The real bootstrap clones two large repos and compiles WRF-SFIRE + WPS,
which takes ~30 minutes. These tests use `dry_run=True` so the install
plan can be verified without doing any heavy work.

Bootstrap is also exposed via `WRFSFireAdapter.bootstrap(...)`. We pin
that pass-through here so callers can rely on the adapter as the single
entry point.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.wrf_sfire_adapter import WRFSFireAdapter
from models.wrf_sfire_bootstrap import (
    BootstrapPlan,
    bootstrap_wrf_sfire_stack,
)


def test_dry_run_plan_lists_all_three_components(tmp_path):
    plan = bootstrap_wrf_sfire_stack(
        install_root=tmp_path / "stack", dry_run=True)
    assert isinstance(plan, BootstrapPlan)
    text = "\n".join(plan.actions)
    assert "WRF-SFIRE" in text
    assert "WPS" in text
    assert "WPS_GEOG" in text


def test_dry_run_actions_include_clone_and_compile(tmp_path):
    plan = bootstrap_wrf_sfire_stack(
        install_root=tmp_path / "stack", dry_run=True)
    actions = "\n".join(plan.actions).lower()
    assert "netcdf_classic=1" in actions
    assert "stdin='32\\n0\\n'" in actions
    assert "clone wrf-sfire" in actions
    assert "compile em_fire" in actions
    assert "compile em_real" in actions
    assert "clone wps" in actions
    assert "compile wps" in actions
    assert "download wps_geog" in actions


def test_dry_run_skip_components(tmp_path):
    plan = bootstrap_wrf_sfire_stack(
        install_root=tmp_path / "stack",
        skip_wrf=True, skip_wps=True, skip_wps_geog=True,
        dry_run=True)
    text = "\n".join(plan.actions).lower()
    assert "skip wrf-sfire" in text
    assert "skip wps " in text
    assert "skip wps_geog" in text


def test_hdf5_env_is_ignored_on_classic_netcdf_path(tmp_path):
    plan = bootstrap_wrf_sfire_stack(
        install_root=tmp_path / "stack",
        netcdf_env={"HDF5": "/usr", "HD5": "/usr"},
        dry_run=True)
    text = "\n".join(plan.actions).lower()
    assert "ignore hdf5/hd5" in text
    assert "netcdf_classic=1" in text


def test_dry_run_detects_existing_install(tmp_path):
    """If the install_root already contains pre-built binaries, the
    plan should skip the compile steps (idempotent)."""
    root = tmp_path / "stack"
    (root / "WRF-SFIRE" / "main").mkdir(parents=True)
    (root / "WPS").mkdir(parents=True)
    (root / "WPS_GEOG").mkdir(parents=True)
    for b in ("wrf.exe", "ideal.exe", "real.exe"):
        (root / "WRF-SFIRE" / "main" / b).write_text("#!/bin/sh\n")
    for b in ("geogrid.exe", "ungrib.exe", "metgrid.exe"):
        (root / "WPS" / b).write_text("#!/bin/sh\n")
    (root / "WPS_GEOG" / "marker").write_text("ok\n")

    plan = bootstrap_wrf_sfire_stack(install_root=root, dry_run=True)
    text = "\n".join(plan.actions).lower()
    assert "binaries already built" in text  # WRF and WPS both
    assert "wps_geog present" in text
    assert "compile em_fire" not in text
    assert "compile em_real" not in text
    assert "compile wps" not in text
    assert "download wps_geog" not in text


def test_adapter_bootstrap_classmethod_passes_through(tmp_path):
    """`WRFSFireAdapter.bootstrap` is the surface-level entry point and
    must produce the same plan as the bootstrap module."""
    plan_from_adapter = WRFSFireAdapter.bootstrap(
        install_root=tmp_path / "via_adapter", dry_run=True)
    plan_from_module = bootstrap_wrf_sfire_stack(
        install_root=tmp_path / "via_module", dry_run=True)
    assert isinstance(plan_from_adapter, BootstrapPlan)
    # Same actions, modulo install root paths.
    assert len(plan_from_adapter.actions) == len(plan_from_module.actions)


def test_install_root_is_created_even_in_dry_run(tmp_path):
    root = tmp_path / "stack_to_be_created"
    assert not root.exists()
    bootstrap_wrf_sfire_stack(install_root=root, dry_run=True)
    assert root.exists() and root.is_dir()
