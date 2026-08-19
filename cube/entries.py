"""Cube entries: what a variable *is* once cascading models can change it.

The cube's job is to let a downstream model ask for `temperature` and get it
as perturbed by whatever ran upstream.  The v2 schema serves that with one row
per variable name and a last-writer-wins payload, which erases the very thing a
cascade is about: "temperature from ERA5" and "temperature after the fire model
perturbed it" are different scientific quantities sharing a name.

Three things follow from that erasure:

- the answer depends on execution order, which the composition engine is meant
  to *decide* rather than inherit;
- what a model actually consumed cannot be reconstructed afterwards, so a
  cascade is not reproducible;
- staleness has to be guessed from wall-clock timestamps, so re-fetching
  identical bytes invalidates everything downstream.

`stage0/runtime_invariants.md` already ruled on this: *"A mutable version such
as `latest` is not sufficient for a later scientific plan"*, and a committed
artifact may not be overwritten.

So an entry is immutable and identified by its content *and* its derivation.
"Latest" stops being a storage property and becomes a query with a declared
policy over the entries that exist.  Each entry carries its own grid, which is
what lets a Lambert intermediate stay in Lambert.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from contracts.identity import strict_hash
from contracts.types import ArtifactDescriptor, GridDescriptor


class ResolutionPolicy(str, Enum):
    """How to choose among the entries that exist for one concept."""

    #: The deepest entry in the cascade -- "temperature as most recently
    #: perturbed".  This is what the blackboard's "latest available" meant,
    #: expressed as a derivation question rather than a write-order accident.
    MOST_DERIVED = "MOST_DERIVED"
    #: The ingested, unperturbed value.  A cascade needs this to answer
    #: "what would it have been without the fire?", which last-writer-wins
    #: cannot answer at all.
    BASELINE = "BASELINE"
    #: Most recently committed, regardless of depth.  Closest to the old
    #: behaviour, and offered so a migration can be exact rather than
    #: approximately equivalent.
    MOST_RECENT = "MOST_RECENT"


class EntryNotFound(KeyError):
    """No entry satisfies the request, and none is invented."""


@dataclass(frozen=True)
class EntryInput:
    """One edge of the cascade: which entry fed which port."""

    port: str
    entry_id: str

    def __post_init__(self) -> None:
        if not self.port:
            raise ValueError("an input edge must name its port")
        if not self.entry_id:
            raise ValueError("an input edge must name its entry")


@dataclass(frozen=True)
class CubeEntry:
    """One immutable value of one concept, with the derivation that made it."""

    entry_id: str
    concept: str
    kind: str
    producer: str
    content_sha256: str
    grid: GridDescriptor | None
    depth: int
    inputs: tuple[EntryInput, ...]
    run_id: str = ""
    committed_at: datetime | None = None
    #: Where the bytes are, and how they are encoded. A catalog that cannot
    #: say where a dataset lives can describe it but never retrieve it.
    #: Deliberately outside `identity_payload`: moving a file or re-recording
    #: its format does not change what the value *is*, and an entry whose id
    #: changed when a file moved would break every lineage edge pointing at it.
    location: str = ""
    media_type: str = ""
    #: Free-form descriptive metadata. Anything not covered by the structured
    #: discovery fields below. Not identity-bearing.
    detail: dict = dataclasses.field(default_factory=dict)
    #: Structured discovery fields. These are what make the catalog
    #: *searchable* rather than merely readable: a consumer asking "what
    #: covers this area, over this window, holding this variable" must not
    #: have to open every file to find out.
    #:
    #: `bbox_lonlat` is deliberately in EPSG:4326 while the payload stays on
    #: its native grid. Discovery needs one comparable frame -- otherwise a
    #: Lambert entry and a UTM entry cannot be compared at all -- and
    #: transforming four corner numbers for an index is not resampling data.
    time_start: str = ""
    time_end: str = ""
    variables: tuple[str, ...] = ()
    bbox_lonlat: tuple[float, float, float, float] | None = None

    def __post_init__(self) -> None:
        for value, label in ((self.concept, "concept"),
                             (self.producer, "producer"),
                             (self.content_sha256, "content_sha256")):
            if not value:
                raise ValueError(f"a cube entry requires a {label}")
        if self.kind not in ("static", "time"):
            raise ValueError("entry kind must be 'static' or 'time'")
        if self.depth < 0:
            raise ValueError("cascade depth cannot be negative")
        if self.grid is not None and not isinstance(self.grid, GridDescriptor):
            raise TypeError("entry grid must be a typed GridDescriptor")
        ports = [item.port for item in self.inputs]
        if len(ports) != len(set(ports)):
            raise ValueError("an entry cannot bind one port twice")
        if self.entry_id and self.entry_id != self.expected_id():
            raise ValueError("cube entry identity does not verify")

    def identity_payload(self) -> dict[str, Any]:
        """What the entry id is computed over.

        Content *and* derivation: two runs of different models over the same
        bytes are different entries, and re-deriving the same value from the
        same inputs is the same entry.  `run_id` and `committed_at` are
        deliberately excluded -- when they are the only difference, the entry
        is the same and should not be duplicated.
        """
        return {
            "schema": "cube-entry-v1",
            "concept": self.concept,
            "kind": self.kind,
            "producer": self.producer,
            "content_sha256": self.content_sha256,
            "grid": None if self.grid is None else self.grid.to_dict(),
            "inputs": [dataclasses.asdict(item)
                       for item in sorted(self.inputs,
                                          key=lambda edge: edge.port)],
        }

    def expected_id(self) -> str:
        return strict_hash(self.identity_payload())

    @classmethod
    def create(cls, *, concept: str, kind: str, producer: str,
               content_sha256: str, grid: GridDescriptor | None = None,
               inputs: tuple[EntryInput, ...] = (), depth: int = 0,
               run_id: str = "",
               committed_at: datetime | None = None,
               location: str = "", media_type: str = "",
               detail: dict | None = None, time_start: str = "",
               time_end: str = "", variables: tuple[str, ...] = (),
               bbox_lonlat: tuple[float, float, float, float] | None = None
               ) -> "CubeEntry":
        """Mint an entry, computing its identity rather than accepting one."""
        draft = cls("", concept, kind, producer, content_sha256, grid, depth,
                    tuple(inputs), run_id, committed_at, location, media_type,
                    dict(detail or {}), time_start, time_end,
                    tuple(variables),
                    None if bbox_lonlat is None else tuple(bbox_lonlat))
        return dataclasses.replace(draft, entry_id=draft.expected_id())

    @property
    def is_baseline(self) -> bool:
        return self.depth == 0 and not self.inputs

    def grid_json(self) -> str:
        return "" if self.grid is None else json.dumps(
            self.grid.to_dict(), sort_keys=True, separators=(",", ":"))


def bbox_lonlat_from_grid(grid: GridDescriptor
                          ) -> tuple[float, float, float, float] | None:
    """Derive a lon/lat discovery box from a grid's own footprint.

    Metadata only. The payload is never touched, never reprojected and never
    resampled; this exists so entries on different native grids can be
    compared in one search.
    """
    try:
        from pyproj import CRS, Transformer
    except ImportError:  # pragma: no cover - pyproj is a hard dependency
        return None
    crs_text = grid.crs
    if crs_text.startswith("WRF-LCC:"):
        params = dict(part.split("=", 1) for part in crs_text.split(":")[1:])
        crs = CRS.from_proj4(
            f"+proj=lcc +lat_1={params['lat_1']} +lat_2={params['lat_2']} "
            f"+lat_0={params['lat_0']} +lon_0={params['lon_0']} "
            f"+x_0=0 +y_0=0 +R={params['R']} +units=m +no_defs")
    else:
        try:
            crs = CRS.from_user_input(crs_text)
        except Exception:
            return None
    minx, miny, maxx, maxy = (float(value) for value in grid.support_bounds)
    to_lonlat = Transformer.from_crs(crs, 4326, always_xy=True)
    xs = [minx, minx, maxx, maxx]
    ys = [miny, maxy, miny, maxy]
    lons, lats = to_lonlat.transform(xs, ys)
    return (min(lons), min(lats), max(lons), max(lats))


def grid_from_json(text: str) -> GridDescriptor | None:
    if not text:
        return None
    return GridDescriptor.from_dict(json.loads(text))


def order_key(policy: ResolutionPolicy):
    """Sort key selecting the winning entry. Total, so ties never float.

    Every policy falls back to committed time and then to `entry_id`, so two
    entries can never tie into a nondeterministic answer.
    """
    epoch = datetime.min

    def most_derived(entry: CubeEntry):
        return (entry.depth, entry.committed_at or epoch, entry.entry_id)

    def baseline(entry: CubeEntry):
        return (-entry.depth, entry.committed_at or epoch, entry.entry_id)

    def most_recent(entry: CubeEntry):
        return (entry.committed_at or epoch, entry.depth, entry.entry_id)

    return {
        ResolutionPolicy.MOST_DERIVED: most_derived,
        ResolutionPolicy.BASELINE: baseline,
        ResolutionPolicy.MOST_RECENT: most_recent,
    }[policy]


@dataclass(frozen=True)
class DatasetRef:
    """A producer's output that is a *file*, not an array.

    WRF-SFIRE writes netCDF; a driver fetches GRIB. Returning one of these
    from a producer says "the result is this file, on this grid" instead of
    "here is an array to write onto the cube's grid". The engine then catalogs
    it where it already lives -- nothing is copied, reprojected, resampled, or
    re-encoded.
    """

    path: str
    media_type: str
    grid: GridDescriptor | None = None
    detail: dict = dataclasses.field(default_factory=dict)
    inputs: tuple[EntryInput, ...] = ()
    #: Full scientific metadata used by the automatic artifact registry.
    #: Legacy catalog-only callers may omit it, but a Cube with artifact
    #: automation enabled fails closed rather than inventing metadata.
    descriptor: ArtifactDescriptor | None = None
    producer_version: str = "unknown"
    output_port_id: str = "result"
    evidence_profile_id: str = "evidence:unknown"
    artifact_inputs: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("a dataset reference must name a path")
        if not self.media_type:
            raise ValueError(
                "a dataset reference must declare its media type, or a "
                "consumer cannot know how to read it")
        if self.grid is not None and not isinstance(self.grid, GridDescriptor):
            raise TypeError("dataset reference grid must be a GridDescriptor")
        if self.descriptor is not None:
            if not isinstance(self.descriptor, ArtifactDescriptor):
                raise TypeError(
                    "dataset reference descriptor must be ArtifactDescriptor")
            if self.descriptor.representation != self.media_type:
                raise ValueError(
                    "dataset media type and descriptor representation disagree")
            if self.grid is not None and self.descriptor.grid != self.grid:
                raise ValueError(
                    "dataset grid and descriptor grid disagree")
        for value, label in (
                (self.producer_version, "producer version"),
                (self.output_port_id, "output port"),
                (self.evidence_profile_id, "evidence profile")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"dataset reference {label} must be text")
        if (not isinstance(self.artifact_inputs, tuple)
                or not all(isinstance(value, tuple) and len(value) == 2
                           and all(isinstance(item, str) and item
                                   for item in value)
                           for value in self.artifact_inputs)):
            raise TypeError(
                "dataset artifact_inputs must be (port_id, artifact_id) pairs")


class ProjectionAuthority:
    """Proof that an artifact was verified against the RuntimeStore.

    The public :meth:`Catalog.commit_entry` already refuses caller-authored
    publication, but the private authoritative committer only replayed the
    projection's *internal* identity: a self-consistent receipt built by hand
    would have published a Cube entry for an artifact that never existed.
    Internal consistency is not authority.

    So the committer now demands one of these, and it can only be minted after
    ``CubeProjector._verify_manifest`` has re-read the manifest bytes, replayed
    the artifact identity, and re-hashed the object.  It is bound to the exact
    artifact it was minted for, so it cannot be replayed against another.

    This mirrors ``acquisition.connector.FetchAuthorization``, which guards the
    fetch boundary the same way.
    """

    __slots__ = ("run_id", "artifact_id", "content_sha256")

    def __init__(self, mint: object, run_id: str, artifact_id: str,
                 content_sha256: str) -> None:
        if mint is not _MINT:
            raise PermissionError(
                "ProjectionAuthority is minted only by a verified "
                "RuntimeStore artifact projection")
        self.run_id = run_id
        self.artifact_id = artifact_id
        self.content_sha256 = content_sha256

    def authorizes(self, run_id: str, artifact_id: str,
                   content_sha256: str) -> bool:
        return (run_id == self.run_id and artifact_id == self.artifact_id
                and content_sha256 == self.content_sha256)


_MINT = object()


def _mint_projection_authority(run_id: str, artifact_id: str,
                               content_sha256: str) -> ProjectionAuthority:
    """Internal: used by :mod:`cube.projection` after verification succeeds."""
    return ProjectionAuthority(_MINT, run_id, artifact_id, content_sha256)


__all__ = [
    "CubeEntry",
    "DatasetRef",
    "EntryInput",
    "EntryNotFound",
    "ProjectionAuthority",
    "ResolutionPolicy",
    "bbox_lonlat_from_grid",
    "grid_from_json",
    "order_key",
]
