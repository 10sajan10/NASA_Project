"""Cross-cube content-addressable cache.

External downloads (Landsat scenes, ARCO-ERA5 chunks, DEM tiles, LANDFIRE
rasters) shouldn't be re-fetched when a new cube root is created. The
cube is per-scenario; the cache is per-machine (or per-cluster on shared
scratch) and survives cube deletion.

Key idea: hash the *query* (source + variable + AOI + time + params) into
a stable SHA-256 and use it as the filename. Same query -> same key ->
same file, deterministically, across cubes / users / machines / runs.

Layout:

    $CUBE_CACHE/
        landsat/
            <hash>.payload         # raw bytes
            <hash>.meta.json       # query, fetched_at, size, content_type
        arco_era5/
            <hash>.payload
            <hash>.meta.json
        ...

Concurrency safe: writes go to `<hash>.tmp` and atomically rename. If two
processes miss the same key simultaneously, both download; the later
rename either wins (last writer; both downloaded identical content
because the key is content-addressable) or finds the file present
(detect via os.path.exists() after rename and skip).

Eviction is LRU by access time on the metadata file (updated on get_path
hit). Caller invokes `evict_lru(target_bytes)` explicitly; the cache
does not auto-evict on writes (too easy to thrash under contention).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


def _canonical_json(obj: Any) -> str:
    """Stable JSON serialization for hash keys.

    Sorted keys, no whitespace, ascii-safe defaults. Tuples become lists,
    datetimes become isoformat strings. Caller is responsible for not
    putting non-JSON-able objects in the query."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str, ensure_ascii=True)


def _hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    """In-memory handle to a cached payload."""
    key: str
    path: Path
    metadata: dict
    size: int


def default_cache_root() -> Path:
    """$CUBE_CACHE if set, else $XDG_CACHE_HOME/nasa_engine, else
    ~/.cache/nasa_engine. Mkdir is the caller's responsibility (or
    ContentCache constructor handles it)."""
    env = os.environ.get("CUBE_CACHE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "nasa_engine"


class ContentCache:
    """Content-addressable on-disk cache.

    Methods are intentionally explicit (key + has + get_path + put_file)
    rather than a single ``fetch_or_compute`` helper, so the caller
    controls *what* gets cached and when. Sources that don't want to
    cache (synthetic weather, in-memory generators) just don't use it.
    """

    def __init__(self, root: Optional[str | Path] = None,
                 *, max_size_bytes: Optional[int] = None) -> None:
        self.root = Path(root) if root is not None else default_cache_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_size_bytes = max_size_bytes

    # ---- key construction ------------------------------------------------
    def key(self, source: str, **params: Any) -> str:
        """SHA-256 of a canonical JSON of (source, sorted params).

        Deterministic and side-effect free; safe to call without
        touching disk. Suitable for sharing across processes/machines."""
        return _hash(_canonical_json({"source": source, "params": params}))

    # ---- low-level paths -------------------------------------------------
    def _bucket(self, source: str) -> Path:
        d = self.root / source
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _payload_path(self, source: str, key: str) -> Path:
        return self._bucket(source) / f"{key}.payload"

    def _meta_path(self, source: str, key: str) -> Path:
        return self._bucket(source) / f"{key}.meta.json"

    # ---- queries ---------------------------------------------------------
    def has(self, source: str, key: str) -> bool:
        return self._payload_path(source, key).exists()

    def get_path(self, source: str, key: str) -> Optional[Path]:
        """Return local path if cached, else None. Updates atime for LRU."""
        p = self._payload_path(source, key)
        if not p.exists():
            return None
        try:
            now = time.time()
            os.utime(p, (now, now))
        except OSError:
            pass
        return p

    def metadata(self, source: str, key: str) -> Optional[dict]:
        m = self._meta_path(source, key)
        if not m.exists():
            return None
        try:
            return json.loads(m.read_text())
        except Exception:
            return None

    # ---- writes ----------------------------------------------------------
    def put_file(self, source: str, key: str,
                 src: str | Path,
                 *,
                 move: bool = False,
                 metadata: Optional[dict] = None) -> Path:
        """Insert a payload file under (source, key). Concurrency-safe:
        each writer gets a unique `mkstemp` tmp file, then atomically
        renames to the target. Content-addressable, so simultaneous puts
        for the same key produce identical content; last rename wins."""
        target = self._payload_path(source, key)
        if target.exists():
            # Already cached. Don't overwrite — content-addressable means
            # the file IS the key; touch atime and return.
            return self.get_path(source, key)  # type: ignore[return-value]

        bucket = self._bucket(source)
        src_path = Path(src)
        if move:
            # Try a direct atomic rename (same filesystem). Falls back to
            # copy+unlink if that fails (e.g. cross-device).
            try:
                os.replace(str(src_path), str(target))
            except OSError:
                fd, tmp = tempfile.mkstemp(dir=str(bucket), suffix=".tmp")
                os.close(fd)
                try:
                    shutil.copyfile(str(src_path), tmp)
                    os.replace(tmp, str(target))
                    src_path.unlink()
                except Exception:
                    if os.path.exists(tmp):
                        try:
                            os.unlink(tmp)
                        except OSError:
                            pass
                    raise
        else:
            fd, tmp = tempfile.mkstemp(dir=str(bucket), suffix=".tmp")
            os.close(fd)
            try:
                shutil.copyfile(str(src_path), tmp)
                os.replace(tmp, str(target))
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                raise

        meta = {
            "source": source,
            "key": key,
            "size": target.stat().st_size,
            "fetched_at": time.time(),
        }
        if metadata:
            meta.update(metadata)
        # Best-effort metadata; don't fail the put if metadata write fails.
        try:
            self._meta_path(source, key).write_text(
                json.dumps(meta, default=str))
        except OSError:
            pass
        return target

    def put_bytes(self, source: str, key: str, data: bytes,
                  *, metadata: Optional[dict] = None) -> Path:
        """Convenience: insert raw bytes."""
        with tempfile.NamedTemporaryFile(
                delete=False, dir=str(self._bucket(source)),
                suffix=".tmp") as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            return self.put_file(source, key, tmp_path,
                                 move=True, metadata=metadata)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # ---- introspection / housekeeping -----------------------------------
    def size_bytes(self) -> int:
        total = 0
        for p in self.root.rglob("*.payload"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def list_entries(self,
                     source: Optional[str] = None) -> list[CacheEntry]:
        out: list[CacheEntry] = []
        roots = [self.root / source] if source else [self.root]
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*.payload"):
                key = p.stem
                src = p.parent.name
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                meta = self.metadata(src, key) or {}
                out.append(CacheEntry(key=key, path=p,
                                       metadata=meta, size=sz))
        return out

    def evict_lru(self, target_bytes: int) -> int:
        """Trim total cache size down to `target_bytes` by deleting
        least-recently-accessed payloads. Returns count evicted."""
        entries = self.list_entries()
        # Sort by atime ascending (oldest first).
        entries.sort(key=lambda e: e.path.stat().st_atime
                     if e.path.exists() else 0)
        current = sum(e.size for e in entries)
        evicted = 0
        for e in entries:
            if current <= target_bytes:
                break
            try:
                e.path.unlink()
                meta = self._meta_path(e.metadata.get("source", ""), e.key)
                if meta.exists():
                    meta.unlink()
            except OSError:
                continue
            current -= e.size
            evicted += 1
        return evicted

    def clear(self, source: Optional[str] = None) -> int:
        """Wipe the cache (or a single source bucket). Returns count removed."""
        target = self.root / source if source else self.root
        if not target.exists():
            return 0
        n = sum(1 for _ in target.rglob("*.payload"))
        if source:
            shutil.rmtree(target)
        else:
            for child in target.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        return n
