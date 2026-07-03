"""Era5GribDriver: ERA5 reanalysis in **GRIB** form for the WPS/ungrib
path (as opposed to the gridded-cube ARCO wind driver in era5_wind.py).

This is the data-driver the WRF-SFIRE model adapter consumes: WPS needs
the full ERA5 column (pressure-level + single-level GRIB) to run
ungrib -> metgrid -> real. The driver:

  1. *Bootstraps its own dependencies.* `write_deps_script()` emits a
     self-contained shell script that installs the CDS API client
     (`cdsapi`) into an isolated venv and verifies the user's
     `~/.cdsapirc` credentials exist. The engine never assumes a host
     already has the right Python or API client — each data driver
     carries its own provisioning, the same principle as the model
     provisioning agent.

  2. *Requests exactly the domain it is asked for.* The download area is
     derived from the cube grid's lat/lon bounds (with padding) or passed
     explicitly as [N, W, S, E]. The time window is chunked (default 5
     days) so a 30-day pull is resumable and never one giant CDS request.

  3. *Maintains a catalog.* Each downloaded chunk is registered in the
     cube catalog under `era5_pressure_grib` / `era5_single_grib`, with
     the GRIB file path recorded in the tile `source`. Re-runs skip
     chunks already on disk + catalogued.

Produces:
  * era5_pressure_grib   pressure-level GRIB chunk files
  * era5_single_grib     single-level (surface/soil) GRIB chunk files
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cube.store import Cube
from cube.catalog import Catalog, TileRecord
from drivers.base import Driver
from engine.log import get_logger

_log = get_logger(__name__)

# ERA5 column WPS needs. Pressure levels span the troposphere/strato so
# real.exe can build the full vertical grid; single levels carry the
# surface/soil state metgrid interpolates to the WRF surface.
_PRESSURE_VARS = [
    "geopotential", "temperature", "u_component_of_wind",
    "v_component_of_wind", "relative_humidity", "vertical_velocity",
]
_PRESSURE_LEVELS = [
    "1000", "975", "950", "925", "900", "875", "850", "825", "800", "775",
    "750", "700", "650", "600", "550", "500", "450", "400", "350", "300",
    "250", "225", "200", "175", "150", "125", "100", "70", "50", "30",
    "20", "10",
]
_SINGLE_VARS = [
    "10m_u_component_of_wind", "10m_v_component_of_wind", "2m_temperature",
    "2m_dewpoint_temperature", "surface_pressure", "skin_temperature",
    "land_sea_mask", "geopotential", "mean_sea_level_pressure",
    "soil_temperature_level_1", "soil_temperature_level_2",
    "soil_temperature_level_3", "soil_temperature_level_4",
    "volumetric_soil_water_layer_1", "volumetric_soil_water_layer_2",
    "volumetric_soil_water_layer_3", "volumetric_soil_water_layer_4",
]

# 6-hourly is the WPS standard for ERA5-forced runs (interval_seconds=21600).
_HOURS = ["00:00", "06:00", "12:00", "18:00"]
_PAD_DEG = 1.0  # extra margin around the cube so metgrid has boundary data


class Era5GribDriver(Driver):
    name = "era5_grib"
    produces = ["era5_pressure_grib", "era5_single_grib"]
    is_static = False

    def __init__(self,
                 *,
                 out_dir: str | Path,
                 area: Optional[list[float]] = None,
                 chunk_days: int = 5,
                 hours: Optional[list[str]] = None,
                 catalog_path: Optional[str | Path] = None) -> None:
        """
        out_dir : where GRIB chunk files are written.
        area : [N, W, S, E] in degrees. If None, derived from the cube grid.
        chunk_days : days per CDS request (keeps requests resumable).
        hours : sub-daily times; default 6-hourly.
        catalog_path : DuckDB catalog to register chunks in; default
                        <out_dir>/era5_catalog.duckdb.
        """
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.area = area
        self.chunk_days = max(1, int(chunk_days))
        self.hours = list(hours) if hours else list(_HOURS)
        self.catalog_path = (Path(catalog_path) if catalog_path
                             else self.out_dir / "era5_catalog.duckdb")

    # ---- dependency bootstrap (the driver's "own api" setup) ------------
    @staticmethod
    def deps_script() -> str:
        """Shell script text that provisions this driver's dependencies."""
        return r"""#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Self-contained dependency bootstrap for the ERA5 GRIB data driver.
# Installs the Copernicus CDS API client into an isolated venv and verifies
# the user's CDS credentials. No host Python packages are assumed.
#
#   usage: era5_grib_deps.sh [VENV_DIR]
# ---------------------------------------------------------------------------
set -euo pipefail
VENV="${1:-$HOME/.era5_driver_venv}"

if [ ! -d "$VENV" ]; then
    echo ">>> creating ERA5 driver venv at $VENV"
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet "cdsapi>=0.7.3" "xarray" "cfgrib" "eccodes"
echo ">>> cdsapi installed: $(python -c 'import cdsapi,sys;print(cdsapi.__version__)')"

# CDS credentials. We never fabricate or paste a key into code; the user
# provides ~/.cdsapirc once (https://cds.climate.copernicus.eu/api-how-to).
if [ ! -f "$HOME/.cdsapirc" ]; then
    cat >&2 <<'MSG'
!!! ~/.cdsapirc not found.
    Create it with your CDS API key (free account):
      url: https://cds.climate.copernicus.eu/api
      key: <UID>:<APIKEY>
    See https://cds.climate.copernicus.eu/how-to-api
MSG
    exit 2
fi
echo ">>> ~/.cdsapirc present — ERA5 driver ready"
"""

    def write_deps_script(self, path: str | Path) -> Path:
        p = Path(path)
        p.write_text(self.deps_script())
        os.chmod(p, 0o755)
        _log.info(f"[era5_grib] wrote deps script -> {p}")
        return p

    def ensure_deps(self) -> None:
        """Import cdsapi; if missing, tell the caller to run deps_script."""
        try:
            import cdsapi  # noqa: F401
        except Exception as e:
            script = self.out_dir / "era5_grib_deps.sh"
            self.write_deps_script(script)
            raise RuntimeError(
                f"cdsapi not importable ({e}). Run the generated deps "
                f"script first:\n    bash {script}\n"
                f"then re-run inside that venv.")

    # ---- area derivation ------------------------------------------------
    def _area_from_cube(self, cube: Cube) -> list[float]:
        """[N, W, S, E] in degrees from the cube grid bounds (+pad)."""
        from pyproj import Transformer
        g = cube.grid
        tr = Transformer.from_crs(g.crs, "EPSG:4326", always_xy=True)
        xs, ys = g.cell_centers_xy()
        import numpy as np
        XX, YY = np.meshgrid([xs[0], xs[-1]], [ys[0], ys[-1]])
        lon, lat = tr.transform(XX, YY)
        N = float(lat.max()) + _PAD_DEG
        S = float(lat.min()) - _PAD_DEG
        W = float(lon.min()) - _PAD_DEG
        E = float(lon.max()) + _PAD_DEG
        return [round(N, 2), round(W, 2), round(S, 2), round(E, 2)]

    # ---- time chunking --------------------------------------------------
    def _chunks(self, t_start: datetime, t_end: datetime):
        """Yield (start, end) windows of <= chunk_days that never cross a
        month boundary (CDS requests take a single year+month)."""
        import calendar
        cur = datetime(t_start.year, t_start.month, t_start.day)
        last = datetime(t_end.year, t_end.month, t_end.day)
        step = timedelta(days=self.chunk_days)
        while cur <= last:
            month_end = datetime(
                cur.year, cur.month,
                calendar.monthrange(cur.year, cur.month)[1])
            chunk_end = min(cur + step - timedelta(days=1), last, month_end)
            yield cur, chunk_end
            cur = chunk_end + timedelta(days=1)

    # ---- engine entrypoint ---------------------------------------------
    def fetch(self, cube: Cube,
              t_start: Optional[datetime] = None,
              t_end: Optional[datetime] = None) -> list[str]:
        if t_start is None or t_end is None:
            raise ValueError("Era5GribDriver requires t_start and t_end")
        self.ensure_deps()
        import cdsapi

        area = self.area or self._area_from_cube(cube)
        _log.info(f"[era5_grib] area [N,W,S,E]={area} "
                  f"{t_start.date()}..{t_end.date()} chunk={self.chunk_days}d")

        cat = Catalog(self.catalog_path)
        cat.register_variable(
            "era5_pressure_grib", kind="grib", units="-",
            description="ERA5 pressure-level GRIB chunk (WPS ungrib input)",
            producer=self.name)
        cat.register_variable(
            "era5_single_grib", kind="grib", units="-",
            description="ERA5 single-level GRIB chunk (WPS ungrib input)",
            producer=self.name)

        client = cdsapi.Client()
        written: list[str] = []
        for c0, c1 in self._chunks(t_start, t_end):
            days = [f"{d:02d}" for d in self._days_in(c0, c1)]
            ym = (c0.year, c0.month)
            pl = self._chunk_path("pressure", c0, c1)
            sl = self._chunk_path("single", c0, c1)

            if not pl.exists():
                _log.info(f"[era5_grib] pressure {c0.date()}..{c1.date()}")
                client.retrieve("reanalysis-era5-pressure-levels", {
                    "product_type": "reanalysis", "variable": _PRESSURE_VARS,
                    "pressure_level": _PRESSURE_LEVELS,
                    "year": str(ym[0]), "month": f"{ym[1]:02d}",
                    "day": days, "time": self.hours, "area": area,
                    "format": "grib",
                }, str(pl))
            if not sl.exists():
                _log.info(f"[era5_grib] single {c0.date()}..{c1.date()}")
                client.retrieve("reanalysis-era5-single-levels", {
                    "product_type": "reanalysis", "variable": _SINGLE_VARS,
                    "year": str(ym[0]), "month": f"{ym[1]:02d}",
                    "day": days, "time": self.hours, "area": area,
                    "format": "grib",
                }, str(sl))

            cat.add_tile(TileRecord("era5_pressure_grib", c0,
                                    source=str(pl), native_res_m=0.25 * 111000))
            cat.add_tile(TileRecord("era5_single_grib", c0,
                                    source=str(sl), native_res_m=0.25 * 111000))
            written += [str(pl), str(sl)]
        cat.close()
        _log.info(f"[era5_grib] {len(written)} GRIB files ready in {self.out_dir}")
        return list(self.produces)

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _days_in(c0: datetime, c1: datetime) -> list[int]:
        # chunk_days is bounded so chunks never straddle a month here
        return list(range(c0.day, c1.day + 1))

    def _chunk_path(self, kind: str, c0: datetime, c1: datetime) -> Path:
        return self.out_dir / (
            f"era5_{kind}_{c0:%Y%m%d}_{c1:%Y%m%d}.grib")
