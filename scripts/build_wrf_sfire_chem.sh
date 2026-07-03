#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Provision a FRESH, WRF-Chem-enabled WRF-SFIRE + WPS stack INSIDE NASA_Project.
#
# Why this exists: the previously compiled WRF/WRF-SFIRE was built WITHOUT
# WRF_CHEM=1, so it cannot emit smoke/aerosol tracers. This script clones a
# clean copy and recompiles with WRF_CHEM=1, using the exact CHPC build
# environment that produced the working (non-chem) build (WRF/wrf_env.sh) +
# NETCDF4 features (NOT classic netcdf).
#
# The engine adapter then points at this stack via --install-root, so the
# model runs through the cascade engine.
#
# Usage:
#   bash scripts/build_wrf_sfire_chem.sh            # full build (~45-90 min)
#   bash scripts/build_wrf_sfire_chem.sh --skip-wps # WRF-SFIRE only
# ---------------------------------------------------------------------------
set -uo pipefail

SAJAN=/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan
PROJECT="$SAJAN/NASA_Project"
STACK="$PROJECT/wrf-sfire-stack"
DEPS="$SAJAN/WRF/dependencies"           # reuse prebuilt jasper/grib2
LOG="$STACK/build.log"

WRF_REPO="https://github.com/openwfm/WRF-SFIRE"
WPS_REPO="https://github.com/openwfm/WPS"

SKIP_WPS=0
[ "${1:-}" = "--skip-wps" ] && SKIP_WPS=1

mkdir -p "$STACK"
exec > >(tee -a "$LOG") 2>&1
echo "================================================================"
echo "WRF-SFIRE (CHEM) build  started $(date)"
echo "install root: $STACK"
echo "================================================================"

# --- ensure Lmod 'module' is available in this (non-interactive) shell ----
if ! type module >/dev/null 2>&1; then
    source "${LMOD_PKG:-/uufs/chpc.utah.edu/sys/installdir/lmod/8.6-r8}/init/bash"
fi

# --- build environment (mirrors WRF/wrf_env.sh) + WRF_CHEM ----------------
module purge
module load gcc/8.5.0 openmpi/4.1.6 hdf5/1.14.1-2 \
            netcdf-c/4.9.2 netcdf-fortran/4.6.1 geotiff/1.4.0

export NETCDF="$(nf-config --prefix)"
export HDF5="$(dirname "$(dirname "$(which h5cc)")")"
export JASPERLIB="$DEPS/grib2/lib"
export JASPERINC="$DEPS/grib2/include"
export LIBTIFF=/usr GEOTIFF=/usr
export LD_LIBRARY_PATH="$NETCDF/lib:$JASPERLIB:${LD_LIBRARY_PATH:-}"

export WRFIO_NCD_LARGE_FILE_SUPPORT=1
export CC=gcc CXX=g++ FC=gfortran F77=gfortran
export FCFLAGS="-m64 -fallow-argument-mismatch"
export FFLAGS="-m64 -fallow-argument-mismatch"
export LDFLAGS="-L$NETCDF/lib -L$JASPERLIB"
export CPPFLAGS="-I$NETCDF/include -I$JASPERINC -fcommon"

# --- WRF-Chem switches ----------------------------------------------------
export WRF_EM_CORE=1
export WRF_NMM_CORE=0
export WRF_CHEM=1          # <-- the whole point: build the chemistry solver
export WRF_KPP=0          # GOCART (chem_opt=17) needs no KPP; avoids flex/yacc

echo "NETCDF = $NETCDF"
echo "HDF5   = $HDF5"
echo "JASPER = $JASPERLIB"
echo "WRF_CHEM=$WRF_CHEM  WRF_KPP=$WRF_KPP"
echo

# =========================================================================
# 1. WRF-SFIRE  (clone + configure + compile em_real with chem)
# =========================================================================
WRF_DIR="$STACK/WRF-SFIRE"
if [ ! -d "$WRF_DIR/.git" ]; then
    echo ">>> cloning WRF-SFIRE (shallow) ..."
    git clone --depth 1 "$WRF_REPO" "$WRF_DIR" || { echo "CLONE FAILED"; exit 1; }
else
    echo ">>> WRF-SFIRE already cloned at $WRF_DIR"
fi

cd "$WRF_DIR"
if [ -x main/wrf.exe ] && [ -x main/real.exe ]; then
    echo ">>> WRF-SFIRE binaries already present (skip compile)"
elif [ -f configure.wrf ]; then
    # Partial build present (objects compiled, link pending). Do NOT clean —
    # that would discard the ~190 compiled chem objects. Just compile again;
    # WRF-Chem often needs a second pass to win the intra-chem .mod race and
    # archive chem into libwrflib.a before linking.
    echo ">>> configure.wrf present + binaries missing -> re-compile (NO clean)"
    grep -q "WRF_CHEM" configure.wrf && echo ">>> chem enabled in configure.wrf"
    ./compile em_real >& compile_em_real.log
else
    echo ">>> configure (option 34 = GNU gfortran/gcc dmpar, basic nesting) ..."
    printf '34\n1\n' | ./configure
    grep -q "WRF_CHEM" configure.wrf && echo ">>> chem enabled in configure.wrf" \
        || echo ">>> WARNING: WRF_CHEM not in configure.wrf"
    echo ">>> compiling em_real WITH chem (long step) ..."
    ./compile em_real >& compile_em_real.log
fi

if [ -x main/wrf.exe ] && [ -x main/real.exe ]; then
    echo ">>> OK: wrf.exe + real.exe built"
    # chem sanity: look for gocart/chem registry symbols
    if grep -qi "chem_opt" Registry/registry.chem 2>/dev/null; then
        echo ">>> chem registry present"
    fi
else
    echo ">>> ERROR: WRF-SFIRE binaries missing — see compile_em_real.log"
    tail -30 compile_em_real.log 2>/dev/null
    exit 2
fi

# =========================================================================
# 2. WPS  (clone + configure + compile)  — reuses prebuilt jasper
# =========================================================================
if [ "$SKIP_WPS" = "0" ]; then
    WPS_DIR="$STACK/WPS"
    if [ ! -d "$WPS_DIR/.git" ]; then
        echo ">>> cloning WPS ..."
        git clone --depth 1 "$WPS_REPO" "$WPS_DIR" || { echo "WPS CLONE FAILED"; exit 1; }
    fi
    cd "$WPS_DIR"
    export WRF_DIR="$WRF_DIR"
    if [ ! -x geogrid.exe ] || [ ! -x metgrid.exe ]; then
        ./clean -a >/dev/null 2>&1 || true
        echo ">>> configure WPS (option 1 = gfortran serial, GRIB2/jasper) ..."
        printf '1\n' | ./configure
        echo ">>> compiling WPS ..."
        ./compile >& compile_wps.log
    fi
    if [ -x geogrid.exe ] && [ -x metgrid.exe ] && [ -x ungrib/src/ungrib.exe ]; then
        echo ">>> OK: geogrid + metgrid + ungrib built"
    else
        echo ">>> ERROR: WPS binaries missing — see compile_wps.log"
        tail -30 compile_wps.log 2>/dev/null
        exit 3
    fi
fi

echo "================================================================"
echo "BUILD COMPLETE $(date)"
echo "  WRF-SFIRE : $WRF_DIR/main/{wrf,real}.exe  (WRF_CHEM=1)"
[ "$SKIP_WPS" = "0" ] && echo "  WPS       : $STACK/WPS/{geogrid,ungrib,metgrid}.exe"
echo "  point the engine at:  --install-root $STACK"
echo "================================================================"
