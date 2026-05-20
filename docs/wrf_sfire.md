# WRF-SFIRE setup notes

This is the install / build / WRFx-context prose that used to live in
`models/wrf_sfire_adapter.py`. The adapter itself is now focused on
runtime translation (cube → namelist + wrfinput → wrfout → cube). See
[models/wrf_sfire_adapter.py](../models/wrf_sfire_adapter.py) for the
runtime contract and
[models/wrf_sfire_bootstrap.py](../models/wrf_sfire_bootstrap.py) for an
automated install.

## What `WRFSFireAdapter` consumes and produces

**Consumes (declared as `DataAdapter` needs):**

| Variable        | Kind   | Use                                                                                       |
|-----------------|--------|-------------------------------------------------------------------------------------------|
| `ignition_t0`   | static | Per-cell asteroid pre-ignition time (s). `NaN` = un-ignited.                              |
| `nfuel_cat`     | static | Anderson 13 fuel category (1..13 burnable, 14 no-fuel).                                   |
| `dem`           | static | Surface elevation on the cube/atmosphere mesh (m).                                        |
| `wind_speed_ms` | time   | Optional. Used to patch `input_sounding` for the ideal-fallback path. Ignored by `real.exe`. |
| `wind_dir_deg`  | time   | Optional. As above.                                                                        |

**Produces:**

| Variable     | Kind   | Origin                                                |
|--------------|--------|-------------------------------------------------------|
| `arrival_s`  | static | `TIGN_G` from `wrfout`, downsampled fire mesh → cube. |
| `fire_area`  | static | `FIRE_AREA` from `wrfout`, downsampled.               |

## How SFIRE knows about the asteroid pulse

- `wrfinput_d01.nc` carries a `TIGN_IN` array on the fire mesh, written
  by the adapter after `real.exe` (or `ideal.exe`).
- The namelist sets `fire_tign_in_time > 0`, which tells SFIRE to read
  `TIGN_IN` and treat every cell with `TIGN_IN < fire_tign_in_time` as
  pre-ignited at that time.
- Cells outside the asteroid footprint get a large sentinel
  (`TIGN_IN >> fire_tign_in_time`) so SFIRE leaves them un-ignited and
  lets its physics propagate fire into them.

Burnability gate: a cell is pre-ignited only if both `ignition_t0` is
finite and `1 ≤ nfuel_cat ≤ 13`. Non-burnable codes (LANDFIRE 91 urban
/ 92 snow / 93 barren / 98 water / 99 nodata — mapped to 14 by
`drivers.landfire_fbfm13`) keep their sentinel even inside the
asteroid annulus.

## Runtime: ideal vs real path

The adapter selects between two paths inside `_make_wrfinput`:

- **`real.exe` path** (chosen automatically when both `real_cmd` and
  `met_em_dir` are configured): WPS-produced `met_em.d01.*` files are
  symlinked into the stage; `real.exe` builds `wrfinput_d01.nc` +
  `wrfbdy_d01` from them. The atmosphere is fully resolved from the
  GRIB → WPS pipeline; cube wind is not a gate on this path.
- **`ideal.exe` path** (everything else): if `ideal_cmd` is set, run
  it; otherwise (test mode) build a minimal NetCDF shell. The cube
  wind, when present, patches `input_sounding` so the ideal column
  starts with the scenario's surface wind.

After either path produces `wrfinput_d01.nc`, the adapter overwrites
`TIGN_IN`, `NFUEL_CAT`, `ZSF` with cube-derived values and runs
`wrf.exe`.

## ZSF on the fire mesh

By default, ZSF is `nearest-neighbour upsample` of the cube `dem`:
every `fmr × fmr` fire cells share one 900 m DEM value. Pass a
`fire_dem_driver` (anything exposing
`fetch_to_array(xmin, ymin, xmax, ymax, width, height, sr)`) to instead
sample DEM at the fire mesh's native resolution — for the Dallas
default (`pixel_m=900`, `fmr=10`) that's 90 m elevation per cell.

## Build / install

The bootstrap module ([models/wrf_sfire_bootstrap.py](../models/wrf_sfire_bootstrap.py))
automates everything below. Manual recipe for reference:

### WRF-SFIRE

```bash
git clone https://github.com/openwfm/wrf-sfire
cd wrf-sfire
module load gcc netcdf-c netcdf-fortran   # or set NETCDF/HDF5 by hand
./configure                                # 34 = GNU dmpar, 1 = basic nesting
./compile em_fire 2>&1 | tee compile_em_fire.log
./compile em_real 2>&1 | tee compile_em_real.log
# main/ should now contain wrf.exe, ideal.exe, and real.exe.
```

### WPS

```bash
cd ..
git clone https://github.com/openwfm/WPS
cd WPS
export WRF_DIR="$(pwd)/../wrf-sfire"
./configure                                # 1 = GNU serial, 17 = Intel serial
./compile 2>&1 | tee compile_wps.log
```

### WPS_GEOG

WPS_GEOG is the static land-use/elevation/soil data `geogrid.exe`
reads. It's multi-tens of GB unpacked.

```bash
cd ..
wget https://demo.openwfm.org/web/wrfx/WPS_GEOG.tbz
tar xvfj WPS_GEOG.tbz
```

### Ubuntu 24.04 quirks

- Ubuntu 24 dropped `libjasper-dev` from main repos. WPS `ungrib`
  needs jasper; the bootstrap module builds jasper-1.900.29 from
  source on demand.
- `$NETCDF` and `$HDF5` need `include/` + `lib/` subdirs. Ubuntu's
  packages don't lay out files that way (HDF5 lives under
  `/usr/include/hdf5/serial/`); the bootstrap auto-creates symlink
  stubs.

## WRFx context

`wrfxpy` / `wrfxweb` / `wrfxctrl` are OpenWFM's optional
orchestration, visualization, and web-submission stack. This
repository has its own engine, cube, drivers, and backends, so
`WRFSFireAdapter` does not need wrfxpy, WRFx queue templates, or WRFx
tokens. Use WRFx only if you want its full forecasting/web workflow
instead of this engine.

Tokens like MesoWest or Earthdata are only needed by the
data-acquisition tools that contact those services; the current ERA5
/ LANDFIRE / DEM drivers here do not use WRFx token files.

## Runtime contract recap

1. WPS_GEOG is used by `geogrid.exe`, not by `WRFSFireAdapter`.
2. WPS produces `met_em.d01.*` files in `met_em_dir`.
3. `WRFSFireAdapter` stages those WRF-native files, runs `real.exe`,
   injects cube-derived fire fields, runs `wrf.exe`, and writes
   outputs back to the cube.

## Test mode (no binaries built)

Pass `ideal_cmd=None` and point `wrf_cmd` at a Python script that
fakes the SFIRE outputs. The adapter's staging logic still runs, so
the end-to-end data flow is validated. See
[tests/test_wrf_sfire_adapter.py](../tests/test_wrf_sfire_adapter.py).
