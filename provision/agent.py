"""WRF-SFIRE provisioning agent.

An autonomous-ish provisioner for the WRF-SFIRE model: it PROBES the host,
DECIDES how to provision a self-contained, chem-enabled WRF-SFIRE + WPS
stack, and EXECUTES that plan — never depending on the host's hand-built
libraries or spack modules.

Strategy selection (system-agnostic by design):

  1. CONTAINER  (preferred — most portable)
       A container runtime exists (apptainer/singularity/podman/docker).
       Build a self-contained image (provision/containers/*) that bundles
       the whole toolchain + I/O stack + WRF-SFIRE/WPS. Runs identically
       anywhere.

  2. SOURCE     (fallback — no container runtime)
       Compilers + make + git + csh present. Build the I/O dependency stack
       FROM SOURCE (build_deps.sh) into a private prefix, then build
       WRF-SFIRE/WPS against it (build_wrf.sh). Still self-contained: brings
       its own zlib/jasper/HDF5/netCDF.

  3. BLOCKED
       Neither possible; the agent reports exactly what is missing.

CLI:
    python -m provision.agent --probe
    python -m provision.agent --plan      [--strategy auto|container|source]
    python -m provision.agent --provision [--install-root ... --deps-prefix ...]

The produced stack lives at INSTALL_ROOT; the cascade engine consumes it via
``run_cascade.py --install-root <INSTALL_ROOT>`` (or the container image),
so provisioning and execution stay on the same engine.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

sys.path.insert(0, str(PROJECT))
from provision.system_probe import SystemProbe  # noqa: E402


@dataclass
class Step:
    """One provisioning action: a human description + the command to run."""
    desc: str
    cmd: list[str]
    cwd: Optional[Path] = None
    env: Optional[dict] = None


@dataclass
class Plan:
    strategy: str                  # "container" | "source" | "blocked"
    steps: list[Step]
    notes: list[str]

    def render(self) -> str:
        L = [f"PROVISIONING PLAN  (strategy = {self.strategy})", "-" * 60]
        for n in self.notes:
            L.append(f"  note: {n}")
        if not self.steps:
            L.append("  (no steps)")
        for i, s in enumerate(self.steps, 1):
            L.append(f"  {i}. {s.desc}")
            L.append(f"       $ {' '.join(s.cmd)}"
                     + (f"   (cwd={s.cwd})" if s.cwd else ""))
        return "\n".join(L)


class WRFSFireProvisioningAgent:
    """Probe -> decide -> plan -> provision."""

    def __init__(self,
                 install_root: str | Path = None,
                 deps_prefix: str | Path = None,
                 chem: bool = True,
                 image_name: str = "wrf-sfire:chem") -> None:
        self.install_root = Path(install_root or (PROJECT / "wrf-sfire-stack"))
        self.deps_prefix = Path(deps_prefix or (PROJECT / "wrfdeps"))
        self.chem = chem
        self.image_name = image_name
        self.probe_result: Optional[SystemProbe] = None

    # ------------------------------------------------------------------
    def probe(self) -> SystemProbe:
        self.probe_result = SystemProbe.probe()
        return self.probe_result

    # ------------------------------------------------------------------
    def decide_strategy(self, prefer: str = "auto") -> str:
        p = self.probe_result or self.probe()
        if prefer in ("container", "source"):
            return prefer
        # auto: container if a runtime exists, else source if toolchain ok
        if p.can_containerize():
            return "container"
        ok, _ = p.can_build_from_source()
        return "source" if ok else "blocked"

    # ------------------------------------------------------------------
    def plan(self, prefer: str = "auto") -> Plan:
        p = self.probe_result or self.probe()
        strat = self.decide_strategy(prefer)
        notes: list[str] = []

        if strat == "container":
            rt = p.best_container_runtime()
            notes.append(f"container runtime: {rt}")
            notes.append("self-contained image: toolchain + I/O stack + "
                         "WRF-SFIRE/WPS built inside; host-independent")
            if rt in ("podman", "docker"):
                steps = [Step(
                    desc=f"build OCI image '{self.image_name}' "
                         f"(deps-from-source + chem WRF + WPS)",
                    cmd=[rt, "build", "-t", self.image_name,
                         "-f", "provision/containers/Dockerfile", "."],
                    cwd=PROJECT)]
                notes.append("for HPC: convert to .sif -> "
                             f"apptainer build wrf.sif docker-daemon://{self.image_name}")
            else:  # apptainer / singularity
                sif = f"{self.image_name.replace(':', '-')}.sif"
                steps = [Step(
                    desc=f"build Apptainer image {sif}",
                    cmd=[rt, "build", sif,
                         "provision/containers/apptainer.def"],
                    cwd=PROJECT)]
            return Plan(strat, steps, notes)

        if strat == "source":
            ok, missing = p.can_build_from_source()
            need_mpi = not p.mpi["mpif90"].present
            if need_mpi:
                notes.append("no MPI wrappers in PATH -> build OpenMPI from "
                             "source too (--with-mpi)")
            notes.append(f"deps prefix : {self.deps_prefix}")
            notes.append(f"install root: {self.install_root}")
            dep_cmd = ["bash", str(HERE / "build_deps.sh"),
                       str(self.deps_prefix)]
            if need_mpi:
                dep_cmd.append("--with-mpi")
            wrf_cmd = ["bash", str(HERE / "build_wrf.sh"),
                       str(self.deps_prefix), str(self.install_root)]
            if not self.chem:
                wrf_cmd.append("--no-chem")
            return Plan(strat, [
                Step("build I/O dependency stack from source", dep_cmd),
                Step("build WRF-SFIRE(+chem) + WPS against it", wrf_cmd),
            ], notes)

        # blocked
        ok, missing = p.can_build_from_source()
        notes.append("no container runtime AND incomplete toolchain")
        notes.append(f"missing for source build: {', '.join(missing)}")
        notes.append("install a container runtime (apptainer recommended) "
                     "or the missing build tools")
        return Plan("blocked", [], notes)

    # ------------------------------------------------------------------
    def provision(self, prefer: str = "auto",
                  execute: bool = False) -> Plan:
        plan = self.plan(prefer)
        print(plan.render())
        if plan.strategy == "blocked":
            return plan
        if not execute:
            print("\n(dry run — pass --provision to execute)")
            return plan
        print("\n=== executing ===")
        for s in plan.steps:
            print(f"\n>>> {s.desc}\n    $ {' '.join(s.cmd)}")
            env = {**os.environ, **(s.env or {})}
            r = subprocess.run(s.cmd, cwd=str(s.cwd) if s.cwd else None,
                               env=env)
            if r.returncode != 0:
                print(f"!!! step failed (rc={r.returncode}); stopping")
                break
        return plan


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true",
                    help="probe the system and print the report")
    ap.add_argument("--plan", action="store_true",
                    help="print the provisioning plan (no execution)")
    ap.add_argument("--provision", action="store_true",
                    help="execute the provisioning plan")
    ap.add_argument("--strategy", default="auto",
                    choices=("auto", "container", "source"))
    ap.add_argument("--install-root", default=None)
    ap.add_argument("--deps-prefix", default=None)
    ap.add_argument("--no-chem", action="store_true")
    ap.add_argument("--image", default="wrf-sfire:chem")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    agent = WRFSFireProvisioningAgent(
        install_root=args.install_root,
        deps_prefix=args.deps_prefix,
        chem=not args.no_chem,
        image_name=args.image)

    if args.probe or not (args.plan or args.provision):
        print(agent.probe().render())
        if not (args.plan or args.provision):
            print("\n" + agent.plan(args.strategy).render())
            return 0

    if args.plan:
        agent.probe()
        print("\n" + agent.plan(args.strategy).render())
        return 0

    if args.provision:
        agent.probe()
        agent.provision(args.strategy, execute=True)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
