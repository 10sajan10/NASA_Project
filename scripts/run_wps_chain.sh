#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# WPS chain for the asteroid-impact 30-day domain:
#   geogrid (3 nests + 90m fire subgrid) -> ungrib PRES -> ungrib SFC -> metgrid
#
#   run_wps_chain.sh [step]    step = geogrid | ungrib | metgrid | all (default all)
# ---------------------------------------------------------------------------
set -uo pipefail
STEP="${1:-all}"

PROJ="/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project"
WPS="$PROJ/wrf-sfire-stack/WPS"
ERA5="$PROJ/runs/asteroid_30day/era5"
NLSRC="$PROJ/runs/asteroid_30day/namelist.wps"
source "$PROJ/wrfdeps/wrf_deps_env.sh"
cd "$WPS"
cp -f "$NLSRC" namelist.wps

run_geogrid() {
    echo "[wps] geogrid $(date)"
    rm -f geo_em.d0*.nc geogrid.log*
    ./geogrid.exe >/dev/null 2>&1 || { echo "[wps] geogrid.exe returned $?"; }
    if ls geo_em.d03.nc >/dev/null 2>&1; then
        echo "[wps] geogrid OK:"; ls -lh geo_em.d0*.nc
    else
        echo "[wps] GEOGRID FAILED — tail geogrid.log:"; tail -20 geogrid.log* 2>/dev/null; return 1
    fi
}

run_ungrib_pass() {  # <prefix> <glob>
    local prefix="$1"; shift
    echo "[wps] ungrib $prefix $(date)"
    sed -i -E "s/^( *prefix *=).*/\1 '$prefix',/" namelist.wps
    rm -f GRIBFILE.* "${prefix}:"* ungrib.log
    ./link_grib.csh "$@"
    ./ungrib.exe >/dev/null 2>&1 || true
    if ls "${prefix}:"* >/dev/null 2>&1; then
        echo "[wps] ungrib $prefix OK ($(ls ${prefix}:* | wc -l) intermediate files)"
    else
        echo "[wps] UNGRIB $prefix FAILED — tail ungrib.log:"; tail -20 ungrib.log 2>/dev/null; return 1
    fi
}

run_metgrid() {
    echo "[wps] metgrid $(date)"
    sed -i -E "s/^( *prefix *=).*/\1 'PRES',/" namelist.wps   # cosmetic restore
    rm -f met_em.d0*.nc metgrid.log*
    ./metgrid.exe >/dev/null 2>&1 || true
    local n=$(ls met_em.d01.* 2>/dev/null | wc -l)
    if [ "$n" -gt 0 ]; then
        echo "[wps] metgrid OK: $(ls met_em.d0*.nc 2>/dev/null | wc -l) met_em files"
        ls met_em.d01.* | head -2
    else
        echo "[wps] METGRID FAILED — tail metgrid.log:"; tail -25 metgrid.log* 2>/dev/null; return 1
    fi
}

case "$STEP" in
    geogrid) run_geogrid ;;
    ungrib)  run_ungrib_pass PRES "$ERA5"/era5_pressure_*.grib && \
             run_ungrib_pass SFC  "$ERA5"/era5_single_*.grib ;;
    metgrid) run_metgrid ;;
    all)
        run_geogrid && \
        run_ungrib_pass PRES "$ERA5"/era5_pressure_*.grib && \
        run_ungrib_pass SFC  "$ERA5"/era5_single_*.grib && \
        run_metgrid && echo "[wps] CHAIN COMPLETE $(date)" ;;
    *) echo "unknown step: $STEP"; exit 1 ;;
esac
