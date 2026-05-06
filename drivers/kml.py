"""Parse thermal-damage KML concentric ring polygons.

The original PDC KML grouped rings by folder names like
"<City> <Band> Thermal Damage". Local scenario KMLs may now contain only one
standalone ring set, so the parser prefers the requested folder when present
and otherwise falls back to the single valid damage-ring set in the file.
Damage-level fluence thresholds come from Acta Astronautica 216 (2024) 468-487,
Table 1, in MJ/m^2 per Mt^(1/6).
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

from lxml import etree
from shapely.geometry import Polygon

KML_NS = "{http://www.opengis.net/kml/2.2}"

DAMAGE_FLUENCE_MJ_M2 = {
    "Serious Burn (2nd-deg)":           0.25,
    "Severe Burn (3rd-deg)":            0.42,
    "Critical Burn (clothing)":         0.84,
    "Unsurvivable Burn (structures)":   1.20,
}


@dataclass
class Ring:
    name: str
    fluence_mj_m2: float
    polygon: Polygon  # EPSG:4326


@dataclass
class CityDamage:
    city: str
    band: str
    rings: list[Ring]  # ascending fluence (outermost first)

    def by_name(self, name: str) -> Ring:
        for r in self.rings:
            if r.name == name:
                return r
        raise KeyError(name)


def _parse_coords(text: str) -> list[tuple[float, float]]:
    pts = []
    for tok in text.strip().split():
        parts = tok.split(",")
        if len(parts) >= 2:
            pts.append((float(parts[0]), float(parts[1])))
    return pts


def _text(el) -> str:
    return el.text.strip() if el is not None and el.text else ""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _folder_name(folder) -> str:
    return _text(folder.find(f"{KML_NS}name"))


def _parse_rings(parent) -> list[Ring]:
    rings: list[Ring] = []
    seen: set[str] = set()
    for pm in parent.findall(f"{KML_NS}Placemark"):
        name = _text(pm.find(f"{KML_NS}name"))
        fluence = DAMAGE_FLUENCE_MJ_M2.get(name)
        if fluence is None:
            continue
        coords_el = pm.find(f".//{KML_NS}coordinates")
        if coords_el is None or not coords_el.text:
            continue
        pts = _parse_coords(coords_el.text)
        if len(pts) < 4:
            continue
        rings.append(Ring(name=name, fluence_mj_m2=fluence,
                          polygon=Polygon(pts)))
        seen.add(name)

    if seen != set(DAMAGE_FLUENCE_MJ_M2):
        return []
    rings.sort(key=lambda r: r.fluence_mj_m2)
    return rings


def _folder_band(folder_name: str, city: str, band: str) -> str:
    suffix = " Thermal Damage"
    if folder_name.endswith(suffix):
        stem = folder_name[:-len(suffix)]
        city_prefix = f"{city} "
        if stem.startswith(city_prefix):
            return stem[len(city_prefix):]
    return band


def load_city_damage(kml_path: Path | str,
                     city: str = "Dallas TX USA",
                     band: str = "Mean") -> CityDamage:
    folder_name = f"{city} {band} Thermal Damage"
    tree = etree.parse(str(kml_path))
    folders = tree.findall(f".//{KML_NS}Folder")
    requested = _normalize(folder_name)

    candidates: list[tuple[str, list[Ring]]] = []
    for folder in folders:
        rings = _parse_rings(folder)
        if rings:
            candidates.append((_folder_name(folder), rings))

    for doc in [tree.getroot(), *tree.findall(f".//{KML_NS}Document")]:
        rings = _parse_rings(doc)
        if rings:
            candidates.append(("standalone thermal damage", rings))

    for name, rings in candidates:
        if _normalize(name) == requested:
            return CityDamage(city=city, band=band, rings=rings)

    city_matches = [
        (name, rings) for name, rings in candidates
        if _normalize(city) in _normalize(name)
        and "thermal damage" in _normalize(name)
    ]
    if len(city_matches) == 1:
        name, rings = city_matches[0]
        return CityDamage(city=city, band=_folder_band(name, city, band),
                          rings=rings)

    if len(candidates) == 1:
        name, rings = candidates[0]
        return CityDamage(city=city, band=_folder_band(name, city, band),
                          rings=rings)

    if not candidates:
        raise ValueError(
            f"no complete thermal damage ring set found in {kml_path}")

    names = ", ".join(repr(name) for name, _ in candidates)
    raise ValueError(
        f"folder not found: {folder_name!r}; found multiple ring sets: {names}")
