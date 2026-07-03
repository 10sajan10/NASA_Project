#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Run WRF-SFIRE(+chem) DIRECTLY on this node (no SLURM) to completion, with
# automatic resume from restart files. Treats the box like a standalone VM:
# uses the cores you give it, loops until the namelist end date is reached,
# and picks up from the latest wrfrst_d01_* checkpoint if a segment dies
# (crash, node reboot -> just re-launch this script).
#
#   run_wrf_direct.sh <run_dir> [NP]
#
#   run_dir  dir with wrf.exe, namelist.input, wrfinput/wrfbdy, tables...
#   NP       MPI ranks (default: adaptive — see below)
#
# Launch detached so it survives logout:
#   nohup scripts/run_wrf_direct.sh <run_dir> 96 > <run_dir>/direct.log 2>&1 &
#   # or inside tmux:  tmux new -s wrf 'scripts/run_wrf_direct.sh <run_dir> 96'
# ---------------------------------------------------------------------------
set -uo pipefail

RUN_DIR="$(readlink -f "${1:?usage: run_wrf_direct.sh <run_dir> [NP]}")"
PROJ="/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project"
DEPS_ENV="$PROJ/wrfdeps/wrf_deps_env.sh"
PREP="$PROJ/scripts/wrf_restart_prep.py"
PYBIN="$PROJ/.venv/bin/python"

cd "$RUN_DIR"
[ -x ./wrf.exe ] || { echo "ERROR: $RUN_DIR/wrf.exe missing"; exit 1; }
[ -f namelist.input ] || { echo "ERROR: namelist.input missing"; exit 1; }
source "$DEPS_ENV"

NCORES=$(nproc)
# CMA shared-memory needs ptrace_scope < 2. If it's 2, OpenMPI must fall back
# to a double-copy mechanism; with that, FEWER ranks is usually faster (less
# memcpy traffic). Pick a sensible default accordingly; caller can override.
SCOPE=$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)
MCA=""
if [ "$SCOPE" = "2" ]; then
    MCA="--mca btl_vader_single_copy_mechanism none"
    DEF_NP=$(awk "BEGIN{n=int($NCORES*0.6); print (n<1?1:n)}")
    echo "[direct] ptrace_scope=2 -> CMA OFF (slow copy fallback)."
    echo "[direct]   TIP: 'sudo sysctl -w kernel.yama.ptrace_scope=0' makes this MUCH faster."
else
    DEF_NP=$(awk "BEGIN{n=int($NCORES*0.85); print (n<1?1:n)}")
    echo "[direct] ptrace_scope=$SCOPE -> CMA ON (fast shared memory)."
fi
NP="${2:-$DEF_NP}"
echo "[direct] node cores=$NCORES  using NP=$NP  run_dir=$RUN_DIR"

SEG=0
while : ; do
    SEG=$((SEG+1))
    "$PYBIN" "$PREP" "$RUN_DIR"; RC=$?
    if [ "$RC" = "3" ]; then
        echo "[direct] run COMPLETE (reached namelist end date)."
        break
    fi
    echo "[direct] === segment $SEG starting $(date) ==="
    mpirun $MCA -np "$NP" ./wrf.exe
    WRC=$?
    echo "[direct] === segment $SEG wrf.exe exited $WRC at $(date) ==="
    # detect clean finish: SUCCESS in rsl OR restart_prep will report done next loop
    if grep -q "SUCCESS COMPLETE WRF" rsl.out.0000 2>/dev/null; then
        echo "[direct] wrf reported SUCCESS COMPLETE."
        # let restart_prep confirm we hit the end date; if not, loop continues
    fi
    # guard against a tight crash loop with no progress
    nrst=$(ls wrfrst_d01_* 2>/dev/null | wc -l)
    if [ "$nrst" -eq 0 ] && [ "$SEG" -ge 3 ]; then
        echo "[direct] no restart files after $SEG segments — aborting to avoid crash loop."
        echo "[direct] check rsl.error.0000:"; tail -20 rsl.error.0000 2>/dev/null
        exit 2
    fi
    sleep 5
done
echo "[direct] done."
