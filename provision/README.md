# WRF-SFIRE Provisioning Agent

A system-agnostic provisioner for the WRF-SFIRE (+WRF-Chem) + WPS model.
It **probes the host**, **decides** how to provision, and **builds
everything self-contained** — never depending on a host's hand-built
libraries or module system.

> Why this exists: the earlier build reused a machine's hand-built jasper
> and spack module paths. That only works on that one machine. This agent
> brings its own dependency stack (or containerizes it), so the same model
> deploys on any cluster, cloud VM, or laptop.

---

## Should you use containers? Yes.

For **system-agnostic, scalable deployment, containers are the right
default.**

| Environment | Use | Why |
|-------------|-----|-----|
| **HPC cluster** | **Apptainer/Singularity** | Runs unprivileged, integrates with the batch scheduler, binds host MPI/interconnect. Docker is normally banned on shared clusters. |
| Cloud / CI / laptop | Docker or Podman | Standard OCI tooling; convert to `.sif` for HPC. |
| No container runtime | **Source build** | Fallback: build the whole I/O stack from source into a private prefix. |

A container builds the toolchain + I/O stack + WRF-SFIRE/WPS **once** and
runs identically everywhere. The source path is the escape hatch when no
runtime is available — it is still self-contained (brings its own
zlib/jasper/HDF5/netCDF, and OpenMPI if needed).

The non-negotiable principle either way: **never depend on a library you
did not build or bundle.**

---

## Usage

```bash
# 1. See what the host can do
python -m provision.agent --probe

# 2. See the chosen plan (auto picks container if a runtime exists)
python -m provision.agent --plan
python -m provision.agent --plan --strategy source     # force source build

# 3. Execute it
python -m provision.agent --provision                  # build the container
python -m provision.agent --provision --strategy source \
    --install-root ./wrf-sfire-stack --deps-prefix ./wrfdeps
```

The probe is pure stdlib, so it runs on a bare login node before any venv
or module load.

---

## What gets built

```
container path                         source path
──────────────                         ───────────
provision/containers/Dockerfile        provision/build_deps.sh  <prefix>
provision/containers/apptainer.def         zlib, libpng, jasper,
   │                                        HDF5, netCDF-C/Fortran,
   ├─ base image: rockylinux:8              [OpenMPI]  -> self-contained
   ├─ build_deps.sh  (I/O stack)        provision/build_wrf.sh   <prefix> <install>
   └─ build_wrf.sh   (WRF+chem+WPS)         WRF-SFIRE(+chem) + WPS
        -> image                            -> <install>/{WRF-SFIRE,WPS}
```

Both paths share the **same two scripts** (`build_deps.sh`,
`build_wrf.sh`), so the container and the bare-metal build are guaranteed
to produce the same stack. `build_wrf.sh` also handles the WRF-Chem
archive race (it links `em_real` in up to three passes, including a manual
`ar` archive of `chem/*.o` into `libwrflib.a`) — the exact failure we hit
compiling against the host stack.

---

## Running it through the engine

Provisioning and execution stay on the same engine:

```bash
# source build -> point the cascade at the produced stack
python scripts/run_cascade.py --config configs/validation_1day.yaml \
    --install-root ./wrf-sfire-stack

# container -> the adapter calls the binaries inside the image
#   (wrf_cmd = apptainer exec <sif> .../wrf.exe), bound to the run dir
```

---

## Files

| File | Role |
|------|------|
| `system_probe.py` | detect container runtimes, compilers, MPI, build tools, libs, Lmod |
| `agent.py` | probe → decide (container/source/blocked) → plan → execute |
| `build_deps.sh` | I/O dependency stack from source into a self-contained prefix |
| `build_wrf.sh` | WRF-SFIRE(+chem)+WPS against that prefix (chem-archive fix included) |
| `containers/apptainer.def` | HPC image definition |
| `containers/Dockerfile` | OCI image (Docker/Podman) |
