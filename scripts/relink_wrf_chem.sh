#!/usr/bin/env bash
# Second-pass link for the WRF-Chem build.
#
# The first ./compile em_real hit an intra-chem dependency race
# (module_mosaic_addemiss compiled before gocart_dust.mod existed), which
# aborted the chem archive step -> chem objects never went into libwrflib.a
# -> wrf.exe link failed on chem symbols. All 190 chem .o now exist and the
# .mod files are built, so:
#   1) re-run ./compile em_real (NO clean) -> compiles the straggler,
#      archives all chem objects, links the binaries.
#   2) if binaries still missing, manually archive chem/*.o into libwrflib.a
#      and compile once more.
set -uo pipefail

SAJAN=/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan
WRF_DIR="$SAJAN/NASA_Project/wrf-sfire-stack/WRF-SFIRE"
DEPS="$SAJAN/WRF/dependencies"

if ! type module >/dev/null 2>&1; then
    source "${LMOD_PKG:-/uufs/chpc.utah.edu/sys/installdir/lmod/8.6-r8}/init/bash"
fi
module purge
module load gcc/8.5.0 openmpi/4.1.6 hdf5/1.14.1-2 \
            netcdf-c/4.9.2 netcdf-fortran/4.6.1 geotiff/1.4.0
export NETCDF="$(nf-config --prefix)"
export HDF5="$(dirname "$(dirname "$(which h5cc)")")"
export JASPERLIB="$DEPS/grib2/lib" JASPERINC="$DEPS/grib2/include"
export LD_LIBRARY_PATH="$NETCDF/lib:$JASPERLIB:${LD_LIBRARY_PATH:-}"
export WRFIO_NCD_LARGE_FILE_SUPPORT=1
export CC=gcc CXX=g++ FC=gfortran F77=gfortran
export FCFLAGS="-m64 -fallow-argument-mismatch" FFLAGS="-m64 -fallow-argument-mismatch"
export LDFLAGS="-L$NETCDF/lib -L$JASPERLIB" CPPFLAGS="-I$NETCDF/include -I$JASPERINC -fcommon"
export WRF_EM_CORE=1 WRF_NMM_CORE=0 WRF_CHEM=1 WRF_KPP=0

cd "$WRF_DIR"
echo ">>> PASS 2: ./compile em_real (no clean)  $(date)"
./compile em_real >& compile_pass2.log
echo ">>> pass 2 done"

if [ -x main/wrf.exe ] && [ -x main/real.exe ]; then
    echo ">>> SUCCESS after pass 2: wrf.exe + real.exe linked"
    exit 0
fi

echo ">>> binaries still missing — manual chem archive + relink"
echo ">>> archiving $(ls chem/*.o | wc -l) chem objects into libwrflib.a"
ar ru main/libwrflib.a chem/*.o
echo ">>> chem objects now in libwrflib.a: $(ar t main/libwrflib.a | grep -c 'chem\|gocart\|mosaic\|sorgam\|aerosol')"
echo ">>> PASS 3: ./compile em_real (link)"
./compile em_real >& compile_pass3.log

if [ -x main/wrf.exe ] && [ -x main/real.exe ]; then
    echo ">>> SUCCESS after manual archive: wrf.exe + real.exe linked"
else
    echo ">>> STILL FAILING — remaining undefined refs:"
    grep "undefined reference" compile_pass3.log | sed 's/.*reference to //' | sort -u | head
    exit 2
fi
