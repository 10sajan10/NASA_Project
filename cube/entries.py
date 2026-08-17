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
from contracts.types import GridDescriptor


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
               committed_at: datetime | None = None) -> "CubeEntry":
        """Mint an entry, computing its identity rather than accepting one."""
        draft = cls("", concept, kind, producer, content_sha256, grid, depth,
                    tuple(inputs), run_id, committed_at)
        return dataclasses.replace(draft, entry_id=draft.expected_id())

    @property
    def is_baseline(self) -> bool:
        return self.depth == 0 and not self.inputs

    def grid_json(self) -> str:
        return "" if self.grid is None else json.dumps(
            self.grid.to_dict(), sort_keys=True, separators=(",", ":"))


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


__all__ = [
    "CubeEntry",
    "EntryInput",
    "EntryNotFound",
    "ResolutionPolicy",
    "grid_from_json",
    "order_key",
]
