"""Cube snapshot utilities.

A snapshot is a byte-for-byte copy of a cube root: grid.json, catalog.duckdb,
catalog.json when present, and all per-variable Zarr stores. Restoring a
snapshot into a run root lets the resolver reuse existing variables instead of
re-fetching or re-predicting them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import shutil
from typing import Iterable

import duckdb


FIRE_OUTPUT_VARIABLES = [
    "ignition_effective_t0",
    "R_head",
    "LB",
    "fireline_intensity_kw_m",
    "arrival_s",
    "fire",
    "population_affected",
]


def resolve_snapshot_path(snapshot: str | Path,
                          snapshot_dir: str | Path = "cube_snapshots") -> Path:
    p = Path(snapshot)
    if p.is_absolute() or len(p.parts) > 1:
        return p
    return Path(snapshot_dir) / p


def _refuse_dangerous_path(path: Path) -> None:
    resolved = path.resolve()
    if resolved == Path("/"):
        raise ValueError("refusing to overwrite filesystem root")
    if len(resolved.parts) < 3:
        raise ValueError(f"refusing to overwrite suspicious path: {resolved}")


def _copy_ignore(_: str, names: list[str]) -> set[str]:
    return {n for n in names if n.endswith(".lock") or n.endswith(".tmp")}


def _remove_path(path: Path) -> None:
    _refuse_dangerous_path(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def save_snapshot(root: str | Path, snapshot: str | Path, *,
                  snapshot_dir: str | Path = "cube_snapshots",
                  overwrite: bool = False) -> Path:
    src = Path(root)
    if not src.exists():
        raise FileNotFoundError(src)

    dest = resolve_snapshot_path(snapshot, snapshot_dir)
    if dest.exists():
        if not overwrite:
            raise FileExistsError(
                f"snapshot already exists: {dest}; use --overwrite-snapshot")
        _remove_path(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=_copy_ignore)

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_root": str(src),
        "snapshot_path": str(dest),
        "format": "cube-root-copy-v1",
    }
    (dest / "snapshot.json").write_text(json.dumps(manifest, indent=2))
    return dest


def restore_snapshot(snapshot: str | Path, root: str | Path, *,
                     snapshot_dir: str | Path = "cube_snapshots",
                     overwrite_root: bool = False) -> Path:
    src = resolve_snapshot_path(snapshot, snapshot_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    if not (src / "grid.json").exists() or not (src / "cube").exists():
        raise ValueError(f"not a cube snapshot/root: {src}")

    dest = Path(root)
    if dest.exists() and dest.is_file():
        if not overwrite_root:
            raise FileExistsError(dest)
        _remove_path(dest)
    elif dest.exists() and any(dest.iterdir()):
        if not overwrite_root:
            raise FileExistsError(
                f"root already exists and is not empty: {dest}; "
                "use --overwrite-root to restore over it")
        _remove_path(dest)
    elif dest.exists():
        # Empty directory; copytree requires the destination not to exist.
        _remove_path(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=_copy_ignore)
    return dest


def drop_cube_variables(root: str | Path, variables: Iterable[str]) -> list[str]:
    """Remove variables from the cube root and catalog.

    This is useful for rerunning downstream models from a restored snapshot:
    keep expensive inputs such as NDVI/NDWI/weather, drop fire outputs, and let
    the resolver recompute only what is missing.
    """
    root = Path(root)
    removed: list[str] = []
    for var in variables:
        path = root / "cube" / f"{var}.zarr"
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(var)

    catalog = root / "catalog.duckdb"
    if catalog.exists():
        con = duckdb.connect(str(catalog))
        try:
            vars_list = list(variables)
            if vars_list:
                con.executemany(
                    "DELETE FROM tiles WHERE variable=?", [(v,) for v in vars_list])
                con.executemany(
                    "DELETE FROM variables WHERE name=?", [(v,) for v in vars_list])
        finally:
            con.close()

    catalog_json = root / "catalog.json"
    if catalog_json.exists():
        catalog_json.unlink()
    return removed
