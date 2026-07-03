#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build WRF-SFIRE (+WRF-Chem) and WPS against a SELF-CONTAINED dependency
# prefix produced by build_deps.sh. No host modules, no hand-built libs.
#
#   build_wrf.sh  <DEPS_PREFIX>  <INSTALL_ROOT>  [--no-chem] [--skip-wps]
#
# Handles the WRF-Chem intra-dependency archive race (chem objects must be
# archived into libwrflib.a before the wrf.exe link) by compiling em_real a
# second time, then a manual `ar` archive fallback — the exact failure mode
# we hit on the first attempt.
# ---------------------------------------------------------------------------
set -uo pipefail

DEPS="${1:?usage: build_wrf.sh <DEPS_PREFIX> <INSTALL_ROOT> [--no-chem] [--skip-wps]}"
INSTALL="${2:?missing INSTALL_ROOT}"
shift 2 || true
CHEM=1; SKIP_WPS=0
for a in "$@"; do
    [ "$a" = "--no-chem" ] && CHEM=0
    [ "$a" = "--skip-wps" ] && SKIP_WPS=1
done

WRF_REPO="${WRF_REPO:-https://github.com/openwfm/WRF-SFIRE}"
WPS_REPO="${WPS_REPO:-https://github.com/openwfm/WPS}"
JOBS="${JOBS:-$(nproc)}"

# --- self-contained environment -------------------------------------------
[ -f "$DEPS/wrf_deps_env.sh" ] || { echo "missing $DEPS/wrf_deps_env.sh — run build_deps.sh first"; exit 1; }
# shellcheck disable=SC1090
source "$DEPS/wrf_deps_env.sh"
export WRF_EM_CORE=1 WRF_NMM_CORE=0
export WRF_CHEM="$CHEM" WRF_KPP=0
export J="-j ${JOBS}"

mkdir -p "$INSTALL"
WRF_DIR="$INSTALL/WRF-SFIRE"

echo "=== WRF-SFIRE build (chem=$CHEM) using deps at $DEPS ==="
echo "NETCDF=$NETCDF  JASPER=$JASPERLIB"

# --- clone -----------------------------------------------------------------
[ -d "$WRF_DIR/.git" ] || git clone --depth 1 "$WRF_REPO" "$WRF_DIR"
cd "$WRF_DIR"

# --- configure (option 34 = GNU gfortran/gcc dmpar, 1 = basic nesting) -----
if [ ! -f configure.wrf ]; then
    printf '34\n1\n' | ./configure
fi
[ "$CHEM" = "1" ] && { grep -q WRF_CHEM configure.wrf && echo "chem enabled" || echo "WARN: chem not in configure.wrf"; }

# --- compile (two passes for the chem archive race) ------------------------
link_ok() { [ -x main/wrf.exe ] && [ -x main/real.exe ]; }

echo ">>> pass 1: compile em_real"
./compile em_real >& compile_pass1.log || true
if ! link_ok; then
    echo ">>> pass 2: compile em_real (resolves chem .mod race + archive)"
    ./compile em_real >& compile_pass2.log || true
fi
if ! link_ok && [ "$CHEM" = "1" ]; then
    echo ">>> manual chem archive into libwrflib.a + relink"
    ar ru main/libwrflib.a chem/*.o
    ./compile em_real >& compile_pass3.log || true
fi

if link_ok; then
    echo ">>> OK: $WRF_DIR/main/{wrf,real}.exe built"
else
    echo ">>> ERROR: WRF binaries missing. Last undefined refs:"
    grep "undefined reference" compile_pass*.log 2>/dev/null | sed 's/.*reference to //' | sort -u | head
    exit 2
fi

# --- WPS -------------------------------------------------------------------
if [ "$SKIP_WPS" = "0" ]; then
    WPS_DIR="$INSTALL/WPS"
    [ -d "$WPS_DIR/.git" ] || git clone --depth 1 "$WPS_REPO" "$WPS_DIR"
    cd "$WPS_DIR"
    export WRF_DIR="$WRF_DIR"
    if [ ! -x geogrid.exe ]; then
        printf '1\n' | ./configure        # 1 = gfortran serial, GRIB2/jasper
        ./compile >& compile_wps.log || true
    fi
    if [ -x geogrid.exe ] && [ -x metgrid.exe ] && [ -x ungrib/src/ungrib.exe ]; then
        echo ">>> OK: WPS geogrid/ungrib/metgrid built"
    else
        echo ">>> ERROR: WPS build failed (see compile_wps.log)"; exit 3
    fi
fi

echo "=== BUILD COMPLETE -> $INSTALL ==="
echo "  point the engine adapter at:  --install-root $INSTALL"
