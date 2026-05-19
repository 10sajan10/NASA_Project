# Wildfire Simulation Engine

A model-agnostic, dependency-resolved, variable-centric orchestration
engine for spatial / spatiotemporal scientific simulations. Plug in any
external model; the engine handles dependency resolution, caching,
parallelism, retries, lineage, and reproducibility.

This repository is the substrate, not a fixed pipeline. There is no
baked-in fire-spread algorithm, no required data source, no scenario
glue. You bring the model and the data; the engine wires them together.

## Components

| Layer | Purpose |
|---|---|
| [cube/](cube/) | Per-variable Zarr storage indexed by a DuckDB catalog. Resolution-aware satisfaction checks, schema versioning, halo I/O, snapshots. |
| [engine/](engine/) | Orchestration substrate. ProducerV2 contract, DAG pipeline DSL, pluggable execution backends (serial / thread / process / dask / SLURM), tile fan-out, retries with dead-letter, dirty propagation, run lineage, content-addressable cache, disk-spill workspace. |
| [drivers/](drivers/) | Data-source-specific fetchers (KML, thermal-pulse, DEM, weather reanalysis, etc). These are scenario-specific — keep what you need, write more as you go. |
| [models/](models/) | `external_model_template.py` shows how to plug a new model in. `wrf_sfire_v2.py` is a worked example (WRF-SFIRE plugged in via `ModelAdapter`). |
| [tests/](tests/) | 158 tests covering every engine guarantee end-to-end. |

## Plugging in a new model

```python
from engine import (DataAdapter, DataNeed, ModelAdapter,
                    ProducerCapabilities, VarSpec, MergePolicy, CostHint)

class MyModel(ModelAdapter):
    name = "my_model"
    data_adapter = DataAdapter([
        DataNeed("ndvi", kind="static", max_native_res_m=30.0),
        DataNeed("wind", kind="time"),
    ])
    produces = (VarSpec("my_output", kind="static",
                         merge_policy=MergePolicy.MONOTONE_MAX),)
    capabilities = ProducerCapabilities(cost_hint=CostHint.CPU)

    def stage_inputs(self, grid, inputs, request, stage_dir):
        ...                                # write inputs to disk
    def run_model(self, stage_dir, request):
        ...                                # invoke binary; return output path
    def parse_outputs(self, output_path, grid):
        ...                                # return {var_name: ndarray}
```

That's the contract. The engine handles:

- **Dependency resolution**: walks `requires` -> `produces` backward from
  any target variable.
- **Resolution-aware cache hits**: `cube.satisfies(spec)` returns True
  only when cached data matches `max_native_res_m`.
- **Dirty propagation**: bumped upstream invalidates downstream
  automatically.
- **Parallel execution**: producers on the same DAG layer fan out across
  the configured backend; tile-aware producers fan tiles across workers.
- **Retries**: configurable `RetryPolicy` with dead-letter tracking.
- **Lineage**: every run records git SHA, config hash, library versions,
  input SHA-256s, and produced variable versions.
- **Cross-cube cache**: `ContentCache` keyed by `SHA-256(source, params)`
  so external fetches survive cube deletion + are shared across runs.

## Worked example: WRF-SFIRE

[models/wrf_sfire_v2.py](models/wrf_sfire_v2.py) shows the full pattern
for an external Fortran model:

1. `DataAdapter` declares `ignition_t0`, `nfuel_cat`, `dem`, optional
   wind/moisture.
2. `stage_inputs` builds the WRF-SFIRE stage directory: copies
   namelist templates, patches them with cube-derived grid/time/
   `fire_tign_in_time`, writes `TIGN_IN` / `NFUEL_CAT` / `ZSF` on the
   fire mesh inside `wrfinput_d01.nc`.
3. `run_model` subprocesses `wrf.exe`.
4. `parse_outputs` extracts `TIGN_G` and `FIRE_AREA` from the wrfout
   and downsamples back to the cube grid.

[tests/test_wrf_sfire_v2.py](tests/test_wrf_sfire_v2.py) validates the
full data flow without requiring a built WRF binary (uses a Python
stand-in subprocess).

## Setup

```bash
./setup.sh                       # creates .venv and installs deps
.venv/bin/python -m pytest tests/
```

## Layout

```
engine/         orchestration substrate (model-agnostic)
cube/           storage + catalog + halo + snapshot
drivers/        data-source fetchers (scenario-specific)
models/         model plug-ins
  external_model_template.py    drop-in template
  wrf_sfire_v2.py               WRF-SFIRE worked example
tests/          engine + adapter integration tests
configs/        example configs
```
