"""WPS meteorology driver.

Downloads ERA5 GRIB data and runs the full WPS chain
(geogrid → ungrib → metgrid) to produce met_em files for real.exe.

Fixes applied vs the original era5gribdataaccess.py:
  - Adds ``geopotential`` to single-level download   → SOILHGT via Vtable
  - Adds ``mean_sea_level_pressure``                 → PMSL / SLP
  - Patches met_em files:
      * SOILHGT = HGT_M  (copy WRF terrain as proxy)
      * SKINTEMP zeros replaced by ST000007

Usage
-----
    from drivers.wps_meteo import WPSMeteoDriver

    driver = WPSMeteoDriver(
        wps_dir   = "wrf-sfire-stack/WPS",
        geog_dir  = "wrf-sfire-stack/WPS_GEOG",
        vtable    = "templates/Vtable.ERA5",
        work_dir  = "data/wps_runs/dallas_2019",
    )
    met_em_dir = driver.run(
        start_date = datetime(2019, 9, 4, 12),
        end_date   = datetime(2019, 9, 11, 12),
        center_lat = 32.78,
        center_lon = -96.81,
        domain_km  = 600,
        dx_m       = 3000,
        e_we       = 201,
        e_sn       = 201,
    )
    # met_em_dir contains met_em.d01.2019-09-04_12:00:00 etc.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from engine.log import get_logger

_log = get_logger(__name__)

# CDS API variable lists (corrected vs original)
_PRESSURE_VARS = [
    "geopotential",
    "temperature",
    "u_component_of_wind",
    "v_component_of_wind",
    "relative_humidity",
    "vertical_velocity",
]

_SINGLE_VARS = [
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "2m_temperature",
    "2m_dewpoint_temperature",
    "surface_pressure",
    "skin_temperature",
    "land_sea_mask",
    "geopotential",               # → SOILHGT (was missing in original)
    "mean_sea_level_pressure",    # → PMSL / SLP (was missing in original)
    "soil_temperature_level_1",
    "soil_temperature_level_2",
    "soil_temperature_level_3",
    "soil_temperature_level_4",
    "volumetric_soil_water_layer_1",
    "volumetric_soil_water_layer_2",
    "volumetric_soil_water_layer_3",
    "volumetric_soil_water_layer_4",
]

_PRESSURE_LEVELS = [
    "1000", "975", "950", "925", "900", "875", "850", "825", "800", "775",
    "750", "700", "650", "600", "550", "500", "450", "400", "350", "300",
    "250", "225", "200", "175", "150", "125", "100", "70", "50", "30",
    "20", "10",
]


class WPSMeteoDriver:
    """Runs the full WPS chain to produce met_em files from ERA5."""

    def __init__(self,
                 *,
                 wps_dir: str | Path,
                 geog_dir: str | Path,
                 vtable: str | Path,
                 work_dir: str | Path,
                 interval_seconds: int = 21600,
                 cds_timeout: int = 3600) -> None:
        """
        wps_dir          : root of compiled WPS (contains geogrid.exe etc.)
        geog_dir         : WPS_GEOG static geography data directory
        vtable           : path to Vtable.ERA5 (ERA-interim compatible)
        work_dir         : scratch directory for this WPS run
        interval_seconds : met_em output interval; must match ERA5 download
                           frequency (default 21600 = 6-hourly)
        cds_timeout      : seconds to wait for each CDS API download
        """
        self.wps_dir = Path(wps_dir).resolve()
        self.geog_dir = Path(geog_dir).resolve()
        self.vtable = Path(vtable).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.interval_seconds = interval_seconds
        self.cds_timeout = cds_timeout

        self._geogrid = self.wps_dir / "geogrid.exe"
        self._ungrib = self.wps_dir / "ungrib" / "src" / "ungrib.exe"
        self._metgrid = self.wps_dir / "metgrid.exe"

        for exe, name in [
            (self._geogrid, "geogrid.exe"),
            (self._ungrib,  "ungrib.exe"),
            (self._metgrid, "metgrid.exe"),
        ]:
            if not exe.exists():
                # Also check top-level WPS dir (some builds put exes there)
                alt = self.wps_dir / name
                if alt.exists():
                    setattr(self, f"_{name.replace('.exe', '')}", alt)
                else:
                    _log.warning("WPS binary not found: %s", exe)

    # ------------------------------------------------------------------ public

    def run(self,
            *,
            start_date: datetime,
            end_date: datetime,
            center_lat: float,
            center_lon: float,
            domain_km: float = 600.0,
            dx_m: float = 3000.0,
            e_we: int = 201,
            e_sn: int = 201,
            map_proj: str = "lambert",
            area_pad_deg: float = 2.0,
            skip_download: bool = False) -> Path:
        """Run full WPS chain and return the met_em output directory.

        Parameters
        ----------
        start_date / end_date : simulation period (UTC)
        center_lat/lon        : domain centre in geographic coordinates
        domain_km             : approximate domain side length (km)
        dx_m / e_we / e_sn    : WRF grid spacing and grid size
        map_proj              : WRF map projection (lambert, mercator, lat-lon)
        area_pad_deg          : extra padding around domain for ERA5 bbox
        skip_download         : reuse existing GRIB files if True
        """
        self.work_dir.mkdir(parents=True, exist_ok=True)
        grib_dir = self.work_dir / "grib"
        grib_dir.mkdir(exist_ok=True)
        met_em_dir = self.work_dir / "met_em"
        met_em_dir.mkdir(exist_ok=True)

        # ------------------------------------------------------------------
        # 1. Download ERA5 GRIB
        # ------------------------------------------------------------------
        pres_grib = grib_dir / "era5_pressure_levels.grib"
        sfc_grib  = grib_dir / "era5_single_levels.grib"

        bbox = _compute_bbox(center_lat, center_lon,
                              domain_km, pad_deg=area_pad_deg)

        if not skip_download or not pres_grib.exists():
            _log.info("Downloading ERA5 pressure-level GRIB …")
            self._download_era5(
                dataset="reanalysis-era5-pressure-levels",
                variables=_PRESSURE_VARS,
                pressure_levels=_PRESSURE_LEVELS,
                start=start_date, end=end_date,
                bbox=bbox,
                out_path=pres_grib,
            )
        else:
            _log.info("Skipping ERA5 pressure download (file exists)")

        if not skip_download or not sfc_grib.exists():
            _log.info("Downloading ERA5 single-level GRIB …")
            self._download_era5(
                dataset="reanalysis-era5-single-levels",
                variables=_SINGLE_VARS,
                start=start_date, end=end_date,
                bbox=bbox,
                out_path=sfc_grib,
            )
        else:
            _log.info("Skipping ERA5 surface download (file exists)")

        # ------------------------------------------------------------------
        # 2. Write namelist.wps
        # ------------------------------------------------------------------
        nml_path = self.work_dir / "namelist.wps"
        self._write_namelist_wps(
            path=nml_path,
            start_date=start_date,
            end_date=end_date,
            center_lat=center_lat,
            center_lon=center_lon,
            dx_m=dx_m,
            e_we=e_we,
            e_sn=e_sn,
            map_proj=map_proj,
        )

        # ------------------------------------------------------------------
        # 3. Symlink Vtable
        # ------------------------------------------------------------------
        vtable_link = self.work_dir / "Vtable"
        if vtable_link.exists() or vtable_link.is_symlink():
            vtable_link.unlink()
        vtable_link.symlink_to(self.vtable)

        # ------------------------------------------------------------------
        # 4. geogrid.exe
        # ------------------------------------------------------------------
        _log.info("Running geogrid.exe …")
        self._run_exe(self._geogrid, cwd=self.work_dir,
                       log=self.work_dir / "geogrid.log")
        geo_em = self.work_dir / "geo_em.d01.nc"
        if not geo_em.exists():
            raise RuntimeError(
                f"geogrid.exe did not produce {geo_em}; check geogrid.log")

        # ------------------------------------------------------------------
        # 5. Link GRIB files for ungrib + run ungrib.exe (twice: PRES + SFC)
        # ------------------------------------------------------------------
        _log.info("Running ungrib.exe for pressure levels …")
        self._run_ungrib(grib_dir / "era5_pressure_levels.grib",
                          prefix="PRES")
        _log.info("Running ungrib.exe for single levels …")
        self._run_ungrib(grib_dir / "era5_single_levels.grib",
                          prefix="SFC")

        # ------------------------------------------------------------------
        # 6. metgrid.exe
        # ------------------------------------------------------------------
        _log.info("Running metgrid.exe …")
        self._run_exe(self._metgrid, cwd=self.work_dir,
                       log=self.work_dir / "metgrid.log")

        met_em_files = sorted(self.work_dir.glob("met_em.d01.*"))
        if not met_em_files:
            raise RuntimeError(
                "metgrid.exe produced no met_em.d01.* files; "
                "check metgrid.log")

        # Move / symlink met_em files into met_em_dir
        for src in met_em_files:
            dst = met_em_dir / src.name
            if not dst.exists():
                shutil.move(str(src), dst)

        # ------------------------------------------------------------------
        # 7. Patch met_em files (SOILHGT + SKINTEMP fixes)
        # ------------------------------------------------------------------
        _log.info("Patching met_em files …")
        nco = _find_nco()
        for f in sorted(met_em_dir.glob("met_em.d01.*")):
            self._patch_met_em(f, nco=nco)

        n = len(list(met_em_dir.glob("met_em.d01.*")))
        _log.info("WPS complete — %d met_em files in %s", n, met_em_dir)
        return met_em_dir

    # ------------------------------------------------------------------ ERA5

    def _download_era5(self, *,
                        dataset: str,
                        variables: list[str],
                        start: datetime,
                        end: datetime,
                        bbox: list[float],
                        out_path: Path,
                        pressure_levels: Optional[list[str]] = None) -> None:
        """Download ERA5 GRIB via CDS API (cdsapi must be installed)."""
        try:
            import cdsapi
        except ImportError:
            raise ImportError(
                "cdsapi not installed. Run: pip install cdsapi\n"
                "Also create ~/.cdsapirc with your CDS credentials.")

        # Build list of days in the range
        days: list[str] = []
        cur = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day = end.replace(hour=0, minute=0, second=0, microsecond=0)
        while cur <= end_day:
            days.append(f"{cur.day:02d}")
            cur += timedelta(days=1)
        days = sorted(set(days))

        # 6-hourly times
        hours = [f"{h:02d}:00" for h in range(0, 24, 6)]

        request: dict = {
            "product_type": "reanalysis",
            "variable": variables,
            "year":  f"{start.year:04d}",
            "month": f"{start.month:02d}",
            "day":   days,
            "time":  hours,
            "area":  bbox,          # [N, W, S, E]
            "format": "grib",
        }
        if pressure_levels:
            request["pressure_level"] = pressure_levels

        c = cdsapi.Client()
        c.retrieve(dataset, request, str(out_path))

    # ------------------------------------------------------------------ WPS helpers

    def _write_namelist_wps(self, *,
                              path: Path,
                              start_date: datetime,
                              end_date: datetime,
                              center_lat: float,
                              center_lon: float,
                              dx_m: float,
                              e_we: int,
                              e_sn: int,
                              map_proj: str) -> None:
        """Write namelist.wps for a single domain."""
        fmt = "%Y-%m-%d_%H:%M:%S"
        text = f"""\
 &share
 wrf_core = 'ARW',
 max_dom = 1,
 start_date = '{start_date.strftime(fmt)}',
 end_date   = '{end_date.strftime(fmt)}',
 interval_seconds = {self.interval_seconds},
 io_form_geogrid = 2,
 /

 &geogrid
 parent_id         =   1,
 parent_grid_ratio =   1,
 i_parent_start    =   1,
 j_parent_start    =   1,
 e_we              = {e_we},
 e_sn              = {e_sn},
 geog_data_res     = 'default',
 dx = {dx_m:.1f},
 dy = {dx_m:.1f},
 map_proj = '{map_proj}',
 ref_lat   = {center_lat:.6f},
 ref_lon   = {center_lon:.6f},
 truelat1  = {center_lat:.6f},
 truelat2  = {center_lat:.6f},
 stand_lon = {center_lon:.6f},
 geog_data_path = '{self.geog_dir}',
 /

 &ungrib
 out_format = 'WPS',
 prefix = 'PRES',
 /

 &metgrid
 fg_name = 'PRES','SFC',
 io_form_metgrid = 2,
 /
"""
        path.write_text(text)

    def _run_ungrib(self, grib_file: Path, prefix: str) -> None:
        """Run ungrib.exe for one GRIB file with the given output prefix."""
        # Update the prefix in namelist.wps
        nml_path = self.work_dir / "namelist.wps"
        text = nml_path.read_text()
        text = _replace_namelist_key(text, "prefix", f"'{prefix}'")
        nml_path.write_text(text)

        # Symlink the GRIB file as GRIBFILE.AAA
        grib_link = self.work_dir / "GRIBFILE.AAA"
        if grib_link.exists() or grib_link.is_symlink():
            grib_link.unlink()
        grib_link.symlink_to(grib_file.resolve())

        self._run_exe(self._ungrib, cwd=self.work_dir,
                       log=self.work_dir / f"ungrib_{prefix}.log")

        grib_link.unlink(missing_ok=True)

    def _run_exe(self, exe: Path, *, cwd: Path, log: Path) -> None:
        with open(log, "w") as fh:
            result = subprocess.run(
                [str(exe)], cwd=cwd,
                stdout=fh, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            raise RuntimeError(
                f"{exe.name} failed (rc={result.returncode}); "
                f"see {log}")

    # ------------------------------------------------------------------ patching

    @staticmethod
    def _patch_met_em(path: Path, *, nco: Optional[str]) -> None:
        """Apply SOILHGT and SKINTEMP patches to a met_em file."""
        if nco is None:
            _log.warning(
                "ncap2 not found — skipping met_em patch for %s. "
                "Install NCO: conda install -c conda-forge nco",
                path.name)
            return

        # Patch 1: SOILHGT = HGT_M  (WRF terrain as proxy for source orography)
        subprocess.run(
            [nco, "-s", "SOILHGT=HGT_M", "-A", str(path), str(path)],
            check=True, capture_output=True)

        # Patch 2: Replace zero SKINTEMP with first soil temperature layer
        subprocess.run(
            [nco, "-s",
             "where(SKINTEMP == 0.0f) SKINTEMP=ST000007;",
             "-A", str(path), str(path)],
            check=True, capture_output=True)


# ------------------------------------------------------------------ utilities

def _compute_bbox(center_lat: float, center_lon: float,
                   domain_km: float, pad_deg: float = 2.0) -> list[float]:
    """Return [North, West, South, East] bounding box for CDS API."""
    lat_deg = domain_km / 111.0 / 2.0 + pad_deg
    lon_deg = domain_km / (111.0 * abs(__import__("math").cos(
        __import__("math").radians(center_lat)))) / 2.0 + pad_deg
    return [
        round(center_lat + lat_deg, 2),   # N
        round(center_lon - lon_deg, 2),   # W
        round(center_lat - lat_deg, 2),   # S
        round(center_lon + lon_deg, 2),   # E
    ]


def _find_nco() -> Optional[str]:
    """Find ncap2 from NCO toolkit."""
    import shutil
    return shutil.which("ncap2")


def _replace_namelist_key(text: str, key: str, value: str) -> str:
    import re
    pattern = re.compile(
        r"^(\s*" + re.escape(key) + r"\s*=\s*)([^,\n!]+)",
        re.MULTILINE)
    return pattern.sub(lambda m: m.group(1) + value, text)
