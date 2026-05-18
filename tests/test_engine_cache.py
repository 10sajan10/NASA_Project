"""ContentCache tests: key stability, atomic put, LRU eviction,
cross-cube reuse pattern.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.cache import ContentCache, default_cache_root


# ----------------------------------------------------- keys
def test_key_is_deterministic(tmp_path):
    c = ContentCache(tmp_path)
    k1 = c.key("landsat", scene="L8_1234", asset="red")
    k2 = c.key("landsat", scene="L8_1234", asset="red")
    assert k1 == k2 and len(k1) == 64  # SHA-256 hex


def test_key_order_independent(tmp_path):
    c = ContentCache(tmp_path)
    a = c.key("landsat", scene="X", asset="red")
    b = c.key("landsat", asset="red", scene="X")
    assert a == b


def test_key_distinguishes_params(tmp_path):
    c = ContentCache(tmp_path)
    a = c.key("landsat", scene="X", asset="red")
    b = c.key("landsat", scene="X", asset="nir")
    c2 = c.key("sentinel", scene="X", asset="red")
    assert len({a, b, c2}) == 3


def test_key_handles_nested_params(tmp_path):
    c = ContentCache(tmp_path)
    k = c.key("landsat",
              bbox=(-97.0, 32.0, -96.0, 33.0),
              t_range=("2024-09-01", "2024-09-15"))
    assert isinstance(k, str) and len(k) == 64


# ----------------------------------------------------- get / put
def test_missing_key_returns_none(tmp_path):
    c = ContentCache(tmp_path)
    assert c.get_path("landsat", "doesnotexist") is None
    assert c.has("landsat", "doesnotexist") is False
    assert c.metadata("landsat", "doesnotexist") is None


def test_put_file_round_trip(tmp_path):
    c = ContentCache(tmp_path)
    src = tmp_path / "raw.tif"
    src.write_bytes(b"GeoTIFF placeholder")
    key = c.key("landsat", scene="X")
    path = c.put_file("landsat", key, src,
                      metadata={"scene_id": "X"})
    assert path.exists()
    assert c.has("landsat", key)
    assert c.get_path("landsat", key) == path
    meta = c.metadata("landsat", key)
    assert meta["scene_id"] == "X"
    assert meta["size"] == len(b"GeoTIFF placeholder")


def test_put_bytes_round_trip(tmp_path):
    c = ContentCache(tmp_path)
    key = c.key("synth", id="abc")
    c.put_bytes("synth", key, b"\x00\x01\x02hello")
    assert c.get_path("synth", key).read_bytes() == b"\x00\x01\x02hello"


def test_put_is_idempotent_does_not_overwrite(tmp_path):
    """Same key + same source -> the existing file is returned, not
    overwritten. Content-addressable: the key IS the content."""
    c = ContentCache(tmp_path)
    src = tmp_path / "raw.tif"
    src.write_bytes(b"first")
    key = c.key("landsat", scene="X")
    p1 = c.put_file("landsat", key, src)
    src.write_bytes(b"second")
    p2 = c.put_file("landsat", key, src)
    assert p1 == p2
    assert p1.read_bytes() == b"first"


def test_atime_updated_on_get(tmp_path):
    c = ContentCache(tmp_path)
    src = tmp_path / "raw"
    src.write_bytes(b"x")
    key = c.key("s", id="1")
    p = c.put_file("s", key, src)
    t0 = p.stat().st_atime
    time.sleep(0.05)
    c.get_path("s", key)
    t1 = p.stat().st_atime
    assert t1 >= t0


# ----------------------------------------------------- cross-cube reuse
def test_cross_cube_reuse_pattern(tmp_path):
    """The whole point: two independent cubes resolve the same key
    against the same cache. Cube A puts; Cube B finds it without
    re-downloading."""
    cache_dir = tmp_path / "shared_cache"

    # Cube A run: cache miss, populate.
    cache_a = ContentCache(cache_dir)
    key = cache_a.key("landsat", scene="L8_001", bbox=(-97, 32, -96, 33))
    assert cache_a.get_path("landsat", key) is None
    src = tmp_path / "downloaded.tif"
    src.write_bytes(b"raw_landsat_bytes")
    cache_a.put_file("landsat", key, src,
                     metadata={"scene_id": "L8_001"})

    # Cube B run: independent instance, same cache dir, key matches.
    cache_b = ContentCache(cache_dir)
    key_b = cache_b.key("landsat", scene="L8_001", bbox=(-97, 32, -96, 33))
    assert key == key_b
    p = cache_b.get_path("landsat", key_b)
    assert p is not None
    assert p.read_bytes() == b"raw_landsat_bytes"


def test_default_cache_root_honors_cube_cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CUBE_CACHE", str(tmp_path / "explicit_cache"))
    root = default_cache_root()
    assert root == tmp_path / "explicit_cache"


# ----------------------------------------------------- eviction
def test_evict_lru_trims_to_target(tmp_path):
    c = ContentCache(tmp_path)
    # 5 entries, 1KB each
    for i in range(5):
        c.put_bytes("s", c.key("s", id=i), b"x" * 1024)
        time.sleep(0.01)   # spread access times
    assert c.size_bytes() == 5 * 1024
    # Touch the most recent to set ordering deterministic; oldest goes first
    evicted = c.evict_lru(target_bytes=2 * 1024)
    assert evicted >= 3
    assert c.size_bytes() <= 2 * 1024


def test_evict_lru_no_op_when_under_budget(tmp_path):
    c = ContentCache(tmp_path)
    c.put_bytes("s", c.key("s", id=0), b"x" * 128)
    assert c.evict_lru(target_bytes=10 * 1024) == 0
    assert c.size_bytes() == 128


def test_clear_wipes_source_bucket(tmp_path):
    c = ContentCache(tmp_path)
    c.put_bytes("a", c.key("a", id=1), b"hello")
    c.put_bytes("b", c.key("b", id=1), b"world")
    n = c.clear(source="a")
    assert n == 1
    assert c.has("a", c.key("a", id=1)) is False
    assert c.has("b", c.key("b", id=1)) is True


# ----------------------------------------------------- concurrency
def test_concurrent_puts_dont_corrupt(tmp_path):
    c = ContentCache(tmp_path)
    src = tmp_path / "data"
    src.write_bytes(b"identical_content")
    key = c.key("s", id="contended")

    errors: list[Exception] = []

    def writer():
        try:
            c.put_file("s", key, src)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert errors == []
    assert c.get_path("s", key).read_bytes() == b"identical_content"


# ----------------------------------------------------- list / introspection
def test_list_entries_returns_metadata(tmp_path):
    c = ContentCache(tmp_path)
    for i in range(3):
        c.put_bytes("s", c.key("s", id=i), b"x" * (i + 1) * 100,
                    metadata={"id": i})
    es = c.list_entries(source="s")
    assert len(es) == 3
    assert all(e.metadata.get("source") == "s" for e in es)
