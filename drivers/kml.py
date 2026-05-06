"""Parse the PDC thermal-damage KML and extract concentric ring polygons.

Each <Folder> named "<City> <Band> Thermal Damage" holds 4 <Placemark> polygons,
one per damage level. Damage-level fluence thresholds come from
Acta Astronautica 216 (2024) 468-487, Table 1, in MJ/m^2 per Mt^(1/6).
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
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


def load_city_damage(kml_path: Path | str,
                     city: str = "Dallas TX USA",
                     band: str = "Mean") -> CityDamage:
    folder_name = f"{city} {band} Thermal Damage"
    tree = etree.parse(str(kml_path))
    target = None
    for f in tree.findall(f".//{KML_NS}Folder"):
        n = f.find(f"{KML_NS}name")
        if n is not None and n.text == folder_name:
            target = f
            break
    if target is None:
        raise ValueError(f"folder not found: {folder_name!r}")

    rings: list[Ring] = []
    for pm in target.findall(f"{KML_NS}Placemark"):
        nm = pm.find(f"{KML_NS}name").text
        coords_el = pm.find(f".//{KML_NS}coordinates")
        if coords_el is None:
            continue
        pts = _parse_coords(coords_el.text)
        fl = DAMAGE_FLUENCE_MJ_M2.get(nm)
        if fl is None:
            continue
        rings.append(Ring(name=nm, fluence_mj_m2=fl, polygon=Polygon(pts)))

    rings.sort(key=lambda r: r.fluence_mj_m2)
    return CityDamage(city=city, band=band, rings=rings)
