#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the FULL WRF/WPS I/O dependency stack FROM SOURCE into a self-contained
# prefix. This is the scalable, system-agnostic alternative to relying on a
# host's spack modules or hand-built libraries.
#
#   build_deps.sh  <PREFIX>  [--with-mpi]
#
#   <PREFIX>     install root (e.g. /opt/wrfdeps or $HOME/wrfdeps)
#   --with-mpi   also build OpenMPI from source (use when no MPI is present;
#                inside a container you usually get MPI from the base image)
#
# Builds (pinned, override via env VARS at top): zlib, libpng, jasper, HDF5,
# netCDF-C, netCDF-Fortran, [OpenMPI]. Emits  $PREFIX/wrf_deps_env.sh  to
# source before configuring WRF/WPS. Used identically by the container
# definitions and by the bare-metal source-build path of the agent.
# ---------------------------------------------------------------------------
set -euo pipefail

PREFIX="${1:?usage: build_deps.sh <PREFIX> [--with-mpi]}"
WITH_MPI=0
[ "${2:-}" = "--with-mpi" ] && WITH_MPI=1

# --- pinned versions (override via environment if needed) ------------------
ZLIB_V="${ZLIB_V:-1.3.1}"
LIBPNG_V="${LIBPNG_V:-1.6.43}"
JASPER_V="${JASPER_V:-2.0.33}"
HDF5_V="${HDF5_V:-1.14.3}"
NETCDFC_V="${NETCDFC_V:-4.9.2}"
NETCDFF_V="${NETCDFF_V:-4.6.1}"
OPENMPI_V="${OPENMPI_V:-4.1.6}"

JOBS="${JOBS:-$(nproc)}"
SRC="$PREFIX/_src"
mkdir -p "$PREFIX" "$SRC"

export CC="${CC:-gcc}" CXX="${CXX:-g++}" FC="${FC:-gfortran}" F77="${F77:-gfortran}"
# -fallow-argument-mismatch only exists on gfortran 10+ (and is only NEEDED
# there — older gfortran treats argument mismatches as warnings). Detect the
# version so the build works on both old (8/9) and new (10+) compilers. The
# dependency libs are modern and compile clean either way; the flag matters
# only for legacy WRF code, applied via the emitted env file below.
GFVER="$("$FC" -dumpversion 2>/dev/null | cut -d. -f1)"
MISMATCH=""
[ "${GFVER:-0}" -ge 10 ] 2>/dev/null && MISMATCH="-fallow-argument-mismatch"
export CFLAGS="${CFLAGS:--O2 -fPIC}"
export FCFLAGS="${FCFLAGS:--O2 -fPIC}"
export FFLAGS="$FCFLAGS"
export LD_LIBRARY_PATH="$PREFIX/lib:$PREFIX/lib64:${LD_LIBRARY_PATH:-}"
export PATH="$PREFIX/bin:$PATH"

fetch() {  # fetch <url> <outfile>
    local url="$1" out="$2"
    [ -f "$SRC/$out" ] && return 0
    echo ">>> downloading $out"
    ( cd "$SRC" && { curl -fSL "$url" -o "$out" || wget -O "$out" "$url"; } )
}

echo "================================================================"
echo "WRF dependency stack -> $PREFIX   (jobs=$JOBS, with_mpi=$WITH_MPI)"
echo "================================================================"

# --- OpenMPI (optional) ----------------------------------------------------
if [ "$WITH_MPI" = "1" ] && ! command -v mpif90 >/dev/null 2>&1; then
    fetch "https://download.open-mpi.org/release/open-mpi/v${OPENMPI_V%.*}/openmpi-${OPENMPI_V}.tar.gz" "openmpi-${OPENMPI_V}.tar.gz"
    ( cd "$SRC" && tar xf "openmpi-${OPENMPI_V}.tar.gz" && cd "openmpi-${OPENMPI_V}" \
        && ./configure --prefix="$PREFIX" >/dev/null \
        && make -j"$JOBS" >/dev/null && make install >/dev/null )
    echo ">>> OpenMPI installed"
fi
# from here on WRF wants the MPI wrappers as the compilers
if command -v mpicc >/dev/null 2>&1; then
    export CC=mpicc CXX=mpicxx FC=mpif90 F77=mpif90
fi

# --- zlib ------------------------------------------------------------------
fetch "https://github.com/madler/zlib/releases/download/v${ZLIB_V}/zlib-${ZLIB_V}.tar.gz" "zlib-${ZLIB_V}.tar.gz"
( cd "$SRC" && tar xf "zlib-${ZLIB_V}.tar.gz" && cd "zlib-${ZLIB_V}" \
    && ./configure --prefix="$PREFIX" >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> zlib"

# --- libpng ----------------------------------------------------------------
fetch "https://github.com/glennrp/libpng/archive/refs/tags/v${LIBPNG_V}.tar.gz" "libpng-${LIBPNG_V}.tar.gz"
( cd "$SRC" && tar xf "libpng-${LIBPNG_V}.tar.gz" && cd "libpng-${LIBPNG_V}" \
    && ./configure --prefix="$PREFIX" CPPFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> libpng"

# --- jasper (GRIB2 for WPS ungrib) -----------------------------------------
fetch "https://github.com/jasper-software/jasper/archive/refs/tags/version-${JASPER_V}.tar.gz" "jasper-${JASPER_V}.tar.gz"
( cd "$SRC" && tar xf "jasper-${JASPER_V}.tar.gz" && cd "jasper-version-${JASPER_V}" \
    && mkdir -p build && cd build \
    && cmake -G "Unix Makefiles" -H.. -B. \
         -DCMAKE_INSTALL_PREFIX="$PREFIX" -DJAS_ENABLE_SHARED=true \
         -DJAS_ENABLE_OPENGL=false -DJAS_ENABLE_LIBJPEG=true >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> jasper"

# --- HDF5 (Fortran + high-level, zlib compression) -------------------------
fetch "https://support.hdfgroup.org/ftp/HDF5/releases/hdf5-${HDF5_V%.*}/hdf5-${HDF5_V}/src/hdf5-${HDF5_V}.tar.gz" "hdf5-${HDF5_V}.tar.gz"
( cd "$SRC" && tar xf "hdf5-${HDF5_V}.tar.gz" && cd "hdf5-${HDF5_V}" \
    && ./configure --prefix="$PREFIX" --enable-fortran --enable-hl \
         --with-zlib="$PREFIX" >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> HDF5"

# --- netCDF-C (netcdf4 on hdf5) --------------------------------------------
fetch "https://downloads.unidata.ucar.edu/netcdf-c/${NETCDFC_V}/netcdf-c-${NETCDFC_V}.tar.gz" "netcdf-c-${NETCDFC_V}.tar.gz"
( cd "$SRC" && tar xf "netcdf-c-${NETCDFC_V}.tar.gz" && cd "netcdf-c-${NETCDFC_V}" \
    && CPPFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" LIBS="-lhdf5_hl -lhdf5 -lz" \
       ./configure --prefix="$PREFIX" --enable-netcdf-4 --disable-dap >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> netCDF-C"

# --- netCDF-Fortran --------------------------------------------------------
fetch "https://downloads.unidata.ucar.edu/netcdf-fortran/${NETCDFF_V}/netcdf-fortran-${NETCDFF_V}.tar.gz" "netcdf-fortran-${NETCDFF_V}.tar.gz"
( cd "$SRC" && tar xf "netcdf-fortran-${NETCDFF_V}.tar.gz" && cd "netcdf-fortran-${NETCDFF_V}" \
    && CPPFLAGS="-I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" LIBS="-lnetcdf" \
       ./configure --prefix="$PREFIX" >/dev/null \
    && make -j"$JOBS" >/dev/null && make install >/dev/null )
echo ">>> netCDF-Fortran"

# --- emit the environment file WRF/WPS configure needs ---------------------
cat > "$PREFIX/wrf_deps_env.sh" <<EOF
# Source before configuring WRF-SFIRE / WPS. Self-contained — no host modules.
export WRFDEPS="$PREFIX"
export NETCDF="\$WRFDEPS"
export HDF5="\$WRFDEPS"
export JASPERLIB="\$WRFDEPS/lib"
export JASPERINC="\$WRFDEPS/include"
export PATH="\$WRFDEPS/bin:\$PATH"
export LD_LIBRARY_PATH="\$WRFDEPS/lib:\$WRFDEPS/lib64:\${LD_LIBRARY_PATH:-}"
export WRFIO_NCD_LARGE_FILE_SUPPORT=1
# -fallow-argument-mismatch only on gfortran 10+ (needed for legacy WRF code)
_gfver="\$(gfortran -dumpversion 2>/dev/null | cut -d. -f1)"
if [ "\${_gfver:-0}" -ge 10 ] 2>/dev/null; then
    export FCFLAGS="-fallow-argument-mismatch"
    export FFLAGS="-fallow-argument-mismatch"
fi
EOF

echo "================================================================"
echo "DEPENDENCY STACK COMPLETE -> $PREFIX"
echo "  source $PREFIX/wrf_deps_env.sh   before building WRF/WPS"
echo "================================================================"
