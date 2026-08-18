"""Configuration-only WRF publication dimension preflight.

This module never imports a WRF binary, opens a wrfout file, or submits work.
It reads the already-declared :class:`models.wrf_config.WRFScenario` dimensions
and answers the narrow question that *is* knowable before launch: which array
shape an atmospheric or fire-grid publication must have.

It intentionally does not manufacture a georeference.  WRF's exact projected
origin and CRS are verified later from ``XLONG/XLAT`` and projection metadata
by :mod:`models.wrf_georeference`.  A passing result here is therefore a
dimension gate, not evidence that the output places onto a cube grid.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .wrf_config import WRFScenario


class ConfiguredWrfGridKind(str, Enum):
    ATMOSPHERIC_MASS = "ATMOSPHERIC_MASS"
    FIRE_VALID = "FIRE_VALID"


class ConfiguredPublicationStatus(str, Enum):
    DIMENSIONS_MATCH = "DIMENSIONS_MATCH"
    DIMENSIONS_MISMATCH = "DIMENSIONS_MISMATCH"
    DOMAIN_NOT_CONFIGURED = "DOMAIN_NOT_CONFIGURED"
    FIRE_GRID_NOT_CONFIGURED = "FIRE_GRID_NOT_CONFIGURED"


@dataclass(frozen=True)
class ConfiguredWrfGridMetadata:
    domain_id: int
    kind: ConfiguredWrfGridKind
    shape: tuple[int, int]
    spacing_m: float

    @property
    def georeference_established(self) -> bool:
        """Always false: dimensions cannot establish CRS or origin."""
        return False


@dataclass(frozen=True)
class PlannedWrfPublication:
    variable: str
    domain_id: int
    grid_kind: ConfiguredWrfGridKind
    declared_shape: tuple[int, int]

    def __post_init__(self) -> None:
        if not isinstance(self.variable, str) or not self.variable.strip():
            raise ValueError("WRF publication variable must be non-empty")
        if (isinstance(self.domain_id, bool)
                or not isinstance(self.domain_id, int)
                or self.domain_id < 1):
            raise ValueError("WRF domain_id must be a positive 1-based index")
        if not isinstance(self.grid_kind, ConfiguredWrfGridKind):
            raise TypeError("WRF grid kind must be typed")
        if (len(self.declared_shape) != 2
                or any(isinstance(item, bool) or not isinstance(item, int)
                       or item <= 0 for item in self.declared_shape)):
            raise ValueError("WRF publication shape must be positive (y, x)")


@dataclass(frozen=True)
class ConfiguredPublicationAssessment:
    variable: str
    status: ConfiguredPublicationStatus
    declared_shape: tuple[int, int]
    expected: ConfiguredWrfGridMetadata | None
    detail: str

    @property
    def dimensions_ok(self) -> bool:
        return self.status is ConfiguredPublicationStatus.DIMENSIONS_MATCH

    @property
    def placement_established(self) -> bool:
        """Never infer geospatial placement from a matching shape."""
        return False


@dataclass(frozen=True)
class ConfiguredWrfPublicationPreflight:
    assessments: tuple[ConfiguredPublicationAssessment, ...]

    @property
    def dimensions_ok(self) -> bool:
        return all(item.dimensions_ok for item in self.assessments)

    @property
    def placement_established(self) -> bool:
        return False


def configured_grid_metadata(
        scenario: WRFScenario, domain_id: int,
        kind: ConfiguredWrfGridKind,
) -> ConfiguredWrfGridMetadata:
    """Derive shape/spacing solely from immutable scenario metadata."""
    if not isinstance(scenario, WRFScenario):
        raise TypeError("configured WRF preflight requires a WRFScenario")
    if isinstance(domain_id, bool) or not isinstance(domain_id, int) \
            or not 1 <= domain_id <= scenario.max_dom:
        raise IndexError(f"WRF domain d{domain_id:02d} is not configured")
    if not isinstance(kind, ConfiguredWrfGridKind):
        raise TypeError("configured WRF grid kind must be typed")
    domain = scenario.domains[domain_id - 1]
    if kind is ConfiguredWrfGridKind.ATMOSPHERIC_MASS:
        return ConfiguredWrfGridMetadata(
            domain_id, kind, (domain.ny, domain.nx), float(domain.dx_m))
    if domain.sr < 1:
        raise ValueError(f"WRF domain d{domain_id:02d} has no fire grid")
    return ConfiguredWrfGridMetadata(
        domain_id, kind,
        (domain.ny * domain.sr, domain.nx * domain.sr),
        float(domain.dx_m) / domain.sr,
    )


def preflight_configured_wrf_publications(
        scenario: WRFScenario,
        planned: Iterable[PlannedWrfPublication],
) -> ConfiguredWrfPublicationPreflight:
    assessments: list[ConfiguredPublicationAssessment] = []
    for publication in planned:
        if not isinstance(publication, PlannedWrfPublication):
            raise TypeError(
                "configured WRF preflight requires planned publication values")
        try:
            expected = configured_grid_metadata(
                scenario, publication.domain_id, publication.grid_kind)
        except IndexError as exc:
            assessments.append(ConfiguredPublicationAssessment(
                publication.variable,
                ConfiguredPublicationStatus.DOMAIN_NOT_CONFIGURED,
                publication.declared_shape, None, str(exc)))
            continue
        except ValueError as exc:
            assessments.append(ConfiguredPublicationAssessment(
                publication.variable,
                ConfiguredPublicationStatus.FIRE_GRID_NOT_CONFIGURED,
                publication.declared_shape, None, str(exc)))
            continue
        matches = publication.declared_shape == expected.shape
        assessments.append(ConfiguredPublicationAssessment(
            publication.variable,
            (ConfiguredPublicationStatus.DIMENSIONS_MATCH if matches
             else ConfiguredPublicationStatus.DIMENSIONS_MISMATCH),
            publication.declared_shape, expected,
            (f"declared {publication.declared_shape}; configured "
             f"{expected.kind.value} grid is {expected.shape}"),
        ))
    return ConfiguredWrfPublicationPreflight(tuple(assessments))


__all__ = [
    "ConfiguredPublicationAssessment",
    "ConfiguredPublicationStatus",
    "ConfiguredWrfGridKind",
    "ConfiguredWrfGridMetadata",
    "ConfiguredWrfPublicationPreflight",
    "PlannedWrfPublication",
    "configured_grid_metadata",
    "preflight_configured_wrf_publications",
]
