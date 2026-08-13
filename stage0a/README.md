# Stage 0A — Runtime build-versus-buy spike

Status: **complete** on 2026-08-12.

Decision: **`CUSTOM_LOCAL_ONLY`** for Stage 1. Build a thin project-owned
controller and supervised local-subprocess provider. Do not build a custom
networked worker runtime. Reconsider Dask only for later stateless,
partition-parallel execution after measurements justify it.

The experiment used a private development node. It did not run WRF-SFIRE, MPI,
SLURM, or network data acquisition. Dask Distributed and Parsl were installed
only in a disposable `/tmp` virtual environment; neither was added to project
requirements.

## Evidence

- `results/comparison.json`: common fixture results and conservative score.
- `ADR-0001-runtime-substrate.md`: decision, ownership boundary, limitations,
  and Stage-1 consequences.
- `selected_substrate.json`: machine-readable Stage-1 boundary.
- `conformance.py`: caller-owned identity, resource request, opaque external
  handle, shared fencing/commit test double, and common fixture.
- `providers.py`: disposable thin-local, Dask, and Parsl adapters.

All candidates received the same synthetic workload:

```text
constant(19) ─┐
              ├─ add -> 42
constant(23) ─┘
```
The fixture also checked duplicate/late fencing, real cancellation versus mere
future cancellation, controller-view reconstruction, bounded admission of eight
partitions, caller resource metadata, and persistence of an opaque fake batch
handle. Framework callbacks never committed artifacts.

## Result summary

| Candidate | Score | Required fixture | Main result |
|---|---:|---|---|
| Thin supervised subprocess | 91/100 | Pass | Running process terminated; reconstructed controller view reconciled it |
| Dask Distributed 2026.3.0 | 82/100 | Fail | Future became cancelled, but synchronous work continued; reconstructed view reported `LOST` |
| Parsl 2026.8.10 | 68/100 | Fail | Running future could not be cancelled or stopped; reconstructed view reported `LOST` |

Four points in the external/MPI category were intentionally unavailable to all
candidates because this node has no certified MPI or SLURM execution profile.
The score combines measured checks with the explicitly documented
implementation/operations judgment in `run_spike.py`; it is not a universal
framework benchmark.

## Reproduce the project-environment check

```bash
.venv/bin/python -m stage0a.run_spike \
  --output stage0a/results/project_environment.json
.venv/bin/python -m pytest tests/test_stage0a_conformance.py -q
```

To repeat the optional-framework comparison without changing project
dependencies:

```bash
.venv/bin/python -m venv /tmp/nasa-stage0a-frameworks
/tmp/nasa-stage0a-frameworks/bin/python -m pip install \
  'dask[distributed]==2026.3.0' 'parsl==2026.8.10'
PYTHONPATH="$PWD" /tmp/nasa-stage0a-frameworks/bin/python \
  -m stage0a.run_spike --output stage0a/results/comparison.json
```
