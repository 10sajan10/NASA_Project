"""SLURM job script generation and submission.

Generates a self-contained SLURM batch script that:
  1. Loads the HPC profile's modules
  2. Runs wrf.exe (or any executable) under MPI
  3. Tails rsl.out.0000 for progress

Usage
-----
    from hpc.slurm import SlurmJob
    from hpc.profiles import detect_profile

    profile = detect_profile()
    job = SlurmJob.from_profile(
        profile,
        job_name="wrf_dallas",
        run_dir="/path/to/stage",
        executable="./wrf.exe",
        np=56,
    )
    script_path = job.write("/path/to/stage/submit.slurm")
    job_id = job.submit(script_path)          # returns SLURM job id string
    status = SlurmJob.wait(job_id, poll_s=30) # blocks until done
"""
from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .profiles import HPCProfile


@dataclass
class SlurmJob:
    """Parameters for a single SLURM job."""

    job_name: str
    run_dir: str | Path
    executable: str               # e.g. "./wrf.exe" or full path
    np: int                       # total MPI ranks
    nodes: int
    ntasks_per_node: int
    partition: str
    account: str
    walltime: str                 # HH:MM:SS
    mem: str                      # e.g. "120GB"
    constraint: str = ""
    modules: list[str] = field(default_factory=list)
    mpi_launcher: str = "mpirun"
    extra_directives: list[str] = field(default_factory=list)
    extra_env: dict[str, str] = field(default_factory=dict)
    output_file: str = "slurm-%j.out"
    error_file: str = "slurm-%j.err"

    # ------------------------------------------------------------------ factory
    @classmethod
    def from_profile(cls,
                     profile: HPCProfile,
                     *,
                     job_name: str,
                     run_dir: str | Path,
                     executable: str,
                     np: int | None = None,
                     account: str | None = None) -> "SlurmJob":
        """Build a SlurmJob from an HPCProfile."""
        total_np = np if np is not None else profile.default_np
        return cls(
            job_name=job_name,
            run_dir=run_dir,
            executable=executable,
            np=total_np,
            nodes=profile.slurm_nodes,
            ntasks_per_node=profile.slurm_ntasks_per_node,
            partition=profile.slurm_partition,
            account=account or profile.slurm_account,
            walltime=profile.slurm_walltime,
            mem=profile.slurm_mem,
            constraint=profile.slurm_constraint,
            modules=list(profile.modules),
            mpi_launcher=profile.mpi_launcher,
            extra_env=dict(profile.extra_env),
        )

    # ------------------------------------------------------------------ script
    def render(self) -> str:
        """Return the SLURM batch script as a string."""
        lines = ["#!/bin/bash", ""]

        # SBATCH directives
        directives = [
            f"#SBATCH --job-name={self.job_name}",
            f"#SBATCH --nodes={self.nodes}",
            f"#SBATCH --ntasks-per-node={self.ntasks_per_node}",
            f"#SBATCH --ntasks={self.np}",
            f"#SBATCH --partition={self.partition}",
            f"#SBATCH --time={self.walltime}",
            f"#SBATCH --mem={self.mem}",
            f"#SBATCH --output={self.output_file}",
            f"#SBATCH --error={self.error_file}",
        ]
        if self.account:
            directives.append(f"#SBATCH --account={self.account}")
        if self.constraint:
            directives.append(f"#SBATCH --constraint={self.constraint}")
        for d in self.extra_directives:
            directives.append(f"#SBATCH {d}")

        lines.extend(directives)
        lines.append("")

        # Module loading
        if self.modules:
            lines.append("# Load modules")
            lines.append("module purge")
            for m in self.modules:
                lines.append(f"module load {m}")
            lines.append("")

        # Extra env
        if self.extra_env:
            lines.append("# Environment")
            for k, v in self.extra_env.items():
                lines.append(f"export {k}={v}")
            lines.append("")

        # Navigate to run dir and launch
        lines.append(f"cd {self.run_dir}")
        lines.append("")
        lines.append("echo \"Job started: $(date)\"")
        lines.append("echo \"Running on nodes: $SLURM_NODELIST\"")
        lines.append("echo \"Tasks: $SLURM_NTASKS\"")
        lines.append("")

        mpi_cmd = self._build_mpi_cmd()
        lines.append(f"{mpi_cmd} &")
        lines.append("WRF_PID=$!")
        lines.append("")
        lines.append("# Stream WRF progress to stdout")
        lines.append("sleep 10")
        lines.append("tail -f rsl.out.0000 &")
        lines.append("TAIL_PID=$!")
        lines.append("")
        lines.append("wait $WRF_PID")
        lines.append("WRF_EXIT=$?")
        lines.append("kill $TAIL_PID 2>/dev/null || true")
        lines.append("")
        lines.append("echo \"Job ended: $(date)\"")
        lines.append("echo \"WRF exit code: $WRF_EXIT\"")
        lines.append("exit $WRF_EXIT")

        return "\n".join(lines) + "\n"

    def _build_mpi_cmd(self) -> str:
        exe = self.executable
        launcher = self.mpi_launcher
        if launcher in ("mpirun", "mpiexec"):
            return f"{launcher} -np {self.np} {exe}"
        if launcher == "srun":
            return f"srun --ntasks={self.np} {exe}"
        if launcher == "ibrun":
            return f"ibrun -np {self.np} {exe}"
        return f"{launcher} {exe}"

    # ------------------------------------------------------------------ I/O
    def write(self, path: str | Path) -> Path:
        """Write the rendered script to disk and make it executable."""
        p = Path(path)
        p.write_text(self.render())
        p.chmod(0o755)
        return p

    # ------------------------------------------------------------------ submit
    def submit(self, script_path: str | Path) -> str:
        """Submit the script via sbatch and return the job id string."""
        result = subprocess.run(
            ["sbatch", str(script_path)],
            capture_output=True, text=True, check=True)
        # sbatch output: "Submitted batch job 12345678"
        match = re.search(r"(\d+)", result.stdout)
        if not match:
            raise RuntimeError(
                f"Could not parse job id from sbatch output: {result.stdout!r}")
        return match.group(1)

    # ------------------------------------------------------------------ wait
    @staticmethod
    def wait(job_id: str,
              poll_s: int = 30,
              timeout_s: int = 24 * 3600) -> str:
        """Block until the SLURM job finishes. Returns final state string.

        States: COMPLETED, FAILED, CANCELLED, TIMEOUT, NODE_FAIL, ...
        Raises RuntimeError on timeout.
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            state = SlurmJob._sacct_state(job_id)
            if state and state not in ("PENDING", "RUNNING", "COMPLETING"):
                return state
            time.sleep(poll_s)
        raise RuntimeError(
            f"SLURM job {job_id} did not finish within {timeout_s} s")

    @staticmethod
    def _sacct_state(job_id: str) -> Optional[str]:
        try:
            result = subprocess.run(
                ["sacct", "-j", job_id, "--format=State", "--noheader",
                 "--parsable2"],
                capture_output=True, text=True, check=True)
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            if lines:
                return lines[0].split("|")[0].strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return None

    @staticmethod
    def cancel(job_id: str) -> None:
        """Cancel a SLURM job."""
        subprocess.run(["scancel", job_id], check=True)

    @staticmethod
    def is_slurm_available() -> bool:
        """Return True if sbatch is on PATH."""
        import shutil
        return shutil.which("sbatch") is not None
