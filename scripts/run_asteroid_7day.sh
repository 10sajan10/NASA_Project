#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Full 7-day asteroid-impact WRF-SFIRE+Chem run, end to end, on this node.
# Rebuilds the inputs that were deleted (ERA5 GRIB + met_em), then runs the
# cascade (real.exe -> sfire injection -> wrf.exe) for 7 days at 90% cores.
#
# Designed to run detached (tmux). Each step is gated; a failure stops the
# chain and is visible in the master log.
# ---------------------------------------------------------------------------
set -uo pipefail
PROJ="/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project"
cd "$PROJ"
source wrfdeps/wrf_deps_env.sh
# ptrace_scope=2 on this node -> OpenMPI CMA off; force the copy fallback so
# mpirun doesn't error (no sudo to set ptrace_scope=0).
export OMPI_MCA_btl_vader_single_copy_mechanism=none
# 56 physical cores / 112 logical (HT). Count hwthreads as slots so -np 100
# (90% of logical) is allowed (== mpirun --use-hwthread-cpus).
export OMPI_MCA_hwloc_base_use_hwthreads_as_cpus=1

LOG="$PROJ/logs/asteroid_7day_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$PROJ/logs" "$PROJ/runs/asteroid_30day/era5"
echo "$LOG" > "$PROJ/.asteroid7d_logpath"
exec > >(tee -a "$LOG") 2>&1

echo "=========================================================="
echo "ASTEROID 7-DAY RUN starting $(date)"
echo "cores=$(nproc)  NP=100 (90%)  ptrace_scope=$(cat /proc/sys/kernel/yama/ptrace_scope)"
echo "=========================================================="

if [ "$(ls runs/asteroid_30day/era5/*.grib 2>/dev/null | wc -l)" -ge 4 ]; then
    echo "[1/3] ERA5 GRIB already present — skipping download $(date)"
else
    echo "[1/3] ERA5 GRIB download (Sep 4 -> Sep 11) $(date)"
    .venv/bin/python runs/asteroid_30day/download_era5.py || { echo "ERA5 DOWNLOAD FAILED"; exit 1; }
fi

nmet=$(ls wrf-sfire-stack/WPS/met_em.d0* 2>/dev/null | wc -l)
if [ "$nmet" -gt 0 ]; then
    echo "[2/3] met_em already present ($nmet files) — skipping WPS $(date)"
else
    echo "[2/3] WPS chain geogrid|ungrib|metgrid $(date)"
    bash scripts/run_wps_chain.sh all || { echo "WPS CHAIN FAILED"; exit 1; }
    nmet=$(ls wrf-sfire-stack/WPS/met_em.d0* 2>/dev/null | wc -l)
    echo "[2/3] met_em files produced: $nmet"
    [ "$nmet" -gt 0 ] || { echo "NO met_em PRODUCED"; exit 1; }
fi

echo "[3/3] cascade 7-day WRF-SFIRE+Chem run $(date)"
.venv/bin/python scripts/run_cascade.py \
    --config configs/asteroid_impact_7day.yaml \
    --real \
    --met-em-dir "$PROJ/wrf-sfire-stack/WPS" \
    --install-root "$PROJ/wrf-sfire-stack" \
    --np 100 || { echo "CASCADE RUN FAILED"; exit 1; }

echo "=========================================================="
echo "ASTEROID 7-DAY RUN FINISHED $(date)"
echo "=========================================================="
