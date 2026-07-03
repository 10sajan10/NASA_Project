#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Submit a WRF-SFIRE(+chem) run as a CHAIN of SLURM jobs that resume from
# restart files. Each job runs up to the partition wall limit (3 days on
# notchpeak); when it ends (finished OR wall-killed) the next job resumes
# from the latest wrfrst_d01_* checkpoint. The chain self-terminates when
# the restart time reaches the namelist end date.
#
#   submit_wrf_chain.sh <run_dir> <account> [partition] [walltime]
#
#   run_dir    dir holding wrf.exe, namelist.input, wrfinput/wrfbdy, met...
#   account    SLURM account (e.g. parashar)
#   partition  default: notchpeak
#   walltime   default: 3-00:00:00
#
# Cores: requests a full exclusive node and uses 80% of its cores for MPI.
# MPI:   uses the self-built OpenMPI in wrfdeps (what wrf.exe is linked to);
#        enables fast CMA shared-memory unless the node blocks ptrace.
# ---------------------------------------------------------------------------
set -euo pipefail

RUN_DIR="$(readlink -f "${1:?usage: submit_wrf_chain.sh <run_dir> <account> [partition] [walltime]}")"
ACCOUNT="${2:?need SLURM account}"
PARTITION="${3:-notchpeak}"
WALLTIME="${4:-3-00:00:00}"

PROJ="/uufs/chpc.utah.edu/common/home/parashar-vdc/sajan/NASA_Project"
DEPS_ENV="$PROJ/wrfdeps/wrf_deps_env.sh"
PREP="$PROJ/scripts/wrf_restart_prep.py"
PYBIN="$PROJ/.venv/bin/python"

[ -x "$RUN_DIR/wrf.exe" ] || { echo "ERROR: $RUN_DIR/wrf.exe not found/exec"; exit 1; }
[ -f "$RUN_DIR/namelist.input" ] || { echo "ERROR: namelist.input missing"; exit 1; }

SEG="$RUN_DIR/wrf_segment.slurm"
cat > "$SEG" <<EOF
#!/usr/bin/env bash
#SBATCH --job-name=wrfsfire_chain
#SBATCH --account=$ACCOUNT
#SBATCH --partition=$PARTITION
#SBATCH --nodes=1
#SBATCH --exclusive
#SBATCH --time=$WALLTIME
#SBATCH --output=$RUN_DIR/slurm-%j.out
set -uo pipefail
cd "$RUN_DIR"

# --- MPI environment: the OpenMPI wrf.exe was linked against ---------------
source "$DEPS_ENV"

# --- restart bookkeeping: set restart flag + resume time -------------------
"$PYBIN" "$PREP" "$RUN_DIR"; PREP_RC=\$?
if [ "\$PREP_RC" = "3" ]; then
    echo "[chain] run already complete — nothing to do."
    exit 0
fi

# --- queue the successor NOW (afterany) so a wall-kill is survived ---------
# The successor's restart_prep will detect completion and no-op if we finish.
sbatch --dependency=afterany:\$SLURM_JOB_ID "$SEG" || true

# --- 80% of this node's cores ---------------------------------------------
NCORES=\${SLURM_CPUS_ON_NODE:-\$(nproc)}
NP=\$(awk "BEGIN{printf \"%d\", int(\$NCORES*0.8)}")
[ "\$NP" -lt 1 ] && NP=1
echo "[chain] node cores=\$NCORES  using NP=\$NP"

# --- CMA shared-mem unless this node blocks ptrace (scope 2) ---------------
SCOPE=\$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)
MCA=""
if [ "\$SCOPE" = "2" ]; then
    MCA="--mca btl_vader_single_copy_mechanism none"
    echo "[chain] ptrace_scope=2 -> CMA disabled, using copy fallback (slower)"
else
    echo "[chain] ptrace_scope=\$SCOPE -> CMA shared-memory enabled (fast)"
fi

echo "[chain] starting wrf.exe at \$(date)"
mpirun \$MCA -np \$NP ./wrf.exe
echo "[chain] wrf.exe exited \$? at \$(date)"
EOF
chmod +x "$SEG"

echo "wrote $SEG"
echo "submitting first segment..."
JOB=$(sbatch --parsable "$SEG")
echo "submitted chain head job: $JOB"
echo "monitor: squeue -j $JOB ; tail -f $RUN_DIR/slurm-*.out"
