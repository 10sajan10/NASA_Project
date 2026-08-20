"""Location revisions for immutable scientific Cube entries."""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from cube.catalog import Catalog
from cube.entries import (
    CubeEntry,
    DatasetLocatorConflict,
    DatasetMetadataConflict,
)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _entry(path: Path, payload: bytes, *, detail: dict | None = None
           ) -> CubeEntry:
    return CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256=_digest(payload), location=str(path.resolve()),
        media_type="application/x-netcdf", detail=detail or {"units": "s"},
        variables=("TIGN_G",))


def test_deleted_location_can_be_replaced_without_changing_scientific_id(
        tmp_path):
    payload = b"scientifically identical wrfout bytes"
    old = tmp_path / "old" / "wrfout.nc"
    new = tmp_path / "new" / "wrfout.nc"
    old.parent.mkdir()
    new.parent.mkdir()
    old.write_bytes(payload)
    new.write_bytes(payload)

    catalog_path = tmp_path / "cube.duckdb"
    catalog = Catalog(catalog_path)
    first = catalog.register_dataset(_entry(old, payload), old)
    old.unlink()
    relocated = catalog.register_dataset(_entry(new, payload), new)

    assert relocated.entry_id == first.entry_id
    assert relocated.location == str(new.resolve())
    assert Path(relocated.location).is_file()
    assert len(catalog.entries_for("arrival_s")) == 1
    assert catalog.search(concept="arrival_s")[0].location == str(new.resolve())
    assert [item.location for item in catalog.locator_history(first.entry_id)] == [
        str(old.resolve()), str(new.resolve())]
    assert catalog.current_locator(first.entry_id).location == str(new.resolve())
    catalog.close()

    reopened = Catalog(catalog_path)
    try:
        assert reopened.entry(first.entry_id).location == str(new.resolve())
        assert reopened.search(media_type="application/x-netcdf")[0].location \
            == str(new.resolve())
    finally:
        reopened.close()


def test_a_second_content_valid_live_locator_is_rejected(tmp_path):
    payload = b"same bytes at two live paths"
    first_path = tmp_path / "first.nc"
    second_path = tmp_path / "second.nc"
    first_path.write_bytes(payload)
    second_path.write_bytes(payload)
    catalog = Catalog(tmp_path / "cube.duckdb")
    first = catalog.register_dataset(_entry(first_path, payload), first_path)

    with pytest.raises(DatasetLocatorConflict, match="ambiguous second locator"):
        catalog.register_dataset(_entry(second_path, payload), second_path)

    assert catalog.entry(first.entry_id).location == str(first_path.resolve())
    assert len(catalog.locator_history(first.entry_id)) == 1
    catalog.close()


def test_conflicting_discovery_metadata_is_not_silently_ignored(tmp_path):
    payload = b"one dataset"
    path = tmp_path / "wrfout.nc"
    path.write_bytes(payload)
    catalog = Catalog(tmp_path / "cube.duckdb")
    first = catalog.register_dataset(_entry(path, payload), path)
    conflicting = dataclasses.replace(
        _entry(path, payload), detail={"units": "minutes"})
    assert conflicting.entry_id == first.entry_id

    with pytest.raises(DatasetMetadataConflict, match="different immutable"):
        catalog.register_dataset(conflicting, path)

    assert catalog.entry(first.entry_id).detail == {"units": "s"}
    assert len(catalog.locator_history(first.entry_id)) == 1
    catalog.close()


def test_legacy_inline_location_becomes_locator_history_on_relocation(tmp_path):
    """Pre-v3.3 entries remain readable and migrate lazily, without rewrite."""
    payload = b"legacy registered bytes"
    old = tmp_path / "legacy.nc"
    new = tmp_path / "relocated.nc"
    old.write_bytes(payload)
    new.write_bytes(payload)
    catalog = Catalog(tmp_path / "cube.duckdb")
    legacy = catalog._commit_entry_for_test_fixture(_entry(old, payload))
    assert catalog.current_locator(legacy.entry_id) is None
    assert catalog.entry(legacy.entry_id).location == str(old.resolve())

    old.unlink()
    relocated = catalog.register_dataset(_entry(new, payload), new)

    assert relocated.entry_id == legacy.entry_id
    assert relocated.location == str(new.resolve())
    assert [item.location for item in catalog.locator_history(legacy.entry_id)] \
        == [str(old.resolve()), str(new.resolve())]
    catalog.close()


def test_registration_refuses_a_symbolic_link_without_minting_a_receipt(
        tmp_path):
    payload = b"target bytes"
    target = tmp_path / "target.nc"
    link = tmp_path / "link.nc"
    target.write_bytes(payload)
    link.symlink_to(target)
    catalog = Catalog(tmp_path / "cube.duckdb")
    linked_entry = dataclasses.replace(
        _entry(target, payload), location=str(link.absolute()))

    with pytest.raises(ValueError, match="symbolic link"):
        catalog.register_dataset(linked_entry, link)

    assert catalog.entry(linked_entry.entry_id) is None
    assert catalog.locator_history(linked_entry.entry_id) == ()
    catalog.close()


def test_mutation_during_hashing_cannot_mint_a_locator(
        tmp_path, monkeypatch):
    import artifacts.registry as registry_module
    import cube.catalog as catalog_module

    original = b"stable before registration"
    path = tmp_path / "wrfout.nc"
    path.write_bytes(original)
    entry = _entry(path, original)
    catalog = Catalog(tmp_path / "cube.duckdb")
    real_fstat = registry_module.os.fstat
    calls = 0

    def mutate_after_open(descriptor):
        nonlocal calls
        before = real_fstat(descriptor)
        calls += 1
        if calls == 1:
            path.write_bytes(b"changed while the descriptor is open")
        return before

    monkeypatch.setattr(registry_module.os, "fstat", mutate_after_open)
    # catalog imports the shared helper directly; pin that fact so this test
    # cannot silently exercise a weaker local reader in the future.
    assert catalog_module._hash_file_with_fingerprint is \
        registry_module._hash_file_with_fingerprint
    with pytest.raises(RuntimeError, match="changed while"):
        catalog.register_dataset(entry, path)

    assert catalog.entry(entry.entry_id) is None
    assert catalog.locator_history(entry.entry_id) == ()
    catalog.close()


def test_non_regular_location_cannot_mint_a_locator(tmp_path):
    directory = tmp_path / "looks-like-a-dataset.nc"
    directory.mkdir()
    entry = CubeEntry.create(
        concept="arrival_s", kind="static", producer="wrf_sfire",
        content_sha256=_digest(b"irrelevant"),
        location=str(directory.resolve()), media_type="application/x-netcdf")
    catalog = Catalog(tmp_path / "cube.duckdb")

    with pytest.raises(FileNotFoundError, match="regular file"):
        catalog.register_dataset(entry, directory)

    assert catalog.entry(entry.entry_id) is None
    assert catalog.locator_history(entry.entry_id) == ()
    catalog.close()
