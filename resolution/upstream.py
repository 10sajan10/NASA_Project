"""Folding several discovery layers into the one upstream completeness channel.

Stage 4 gave :class:`~resolution.service.WorkflowResolver` a single pair of
inputs — ``upstream_discovery_complete`` and ``upstream_limit_codes`` — through
which discovery that ran *before* the resolver reports whether it truncated.

Stage 5 adds a second such layer (remote metadata search) on top of the first
(transformation closure).  The temptation is to give the resolver a second
channel.  That would be a mistake: two completeness flags can disagree, and
whichever one the optimality claim forgets to consult becomes a silent bug of
exactly the kind Stage 4 had to fix.

So layers are *folded* here instead.  Completeness is conjunction, truncation
reasons are union, and the result feeds the existing single channel unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class UpstreamCompleteness:
    """The combined verdict of every discovery layer above the resolver."""

    complete: bool
    limit_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.complete) is not bool:
            raise TypeError("upstream completeness must be bool")
        codes = self.limit_codes
        if (not isinstance(codes, tuple)
                or any(not isinstance(value, str) or not value.strip()
                       for value in codes)):
            raise TypeError("upstream limit codes must be non-empty text")
        if codes != tuple(sorted(set(codes))):
            raise ValueError("upstream limit codes must be unique and sorted")
        # The same consistency rule the resolver enforces on its own inputs, so
        # an inconsistent pair cannot be assembled here and passed on later.
        if self.complete and codes:
            raise ValueError(
                "complete upstream discovery cannot report limit codes")
        if not self.complete and not codes:
            raise ValueError(
                "incomplete upstream discovery must name its limit codes")

    @classmethod
    def whole(cls) -> "UpstreamCompleteness":
        """Nothing above the resolver truncated anything."""
        return cls(True, ())

    @classmethod
    def from_layer(cls, complete: bool,
                   limit_codes: Iterable[str] = ()) -> "UpstreamCompleteness":
        """Build a verdict from one layer's own flag and typed reasons."""
        codes = tuple(sorted({value for value in limit_codes}))
        return cls(bool(complete), codes)

    def merge(self, other: "UpstreamCompleteness") -> "UpstreamCompleteness":
        if not isinstance(other, UpstreamCompleteness):
            raise TypeError("merge requires an UpstreamCompleteness")
        return UpstreamCompleteness(
            self.complete and other.complete,
            tuple(sorted(set(self.limit_codes) | set(other.limit_codes))),
        )

    @classmethod
    def merge_all(cls, layers: Iterable["UpstreamCompleteness"]
                  ) -> "UpstreamCompleteness":
        result = cls.whole()
        for layer in layers:
            result = result.merge(layer)
        return result

    def resolver_kwargs(self) -> dict[str, Any]:
        """The exact keyword arguments ``WorkflowResolver`` already accepts."""
        return {
            "upstream_discovery_complete": self.complete,
            "upstream_limit_codes": self.limit_codes,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "limit_codes": list(self.limit_codes),
        }


__all__ = ["UpstreamCompleteness"]
