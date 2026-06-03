# Asteroid-Impact Cascade — System Architecture

A scalable, infrastructure-independent environmental modelling system for
predicting the **cascading effects of an asteroid impact**. The immediate
model is **WRF-SFIRE (wildfire)**; the architecture is designed so further
models (smoke dispersion, flooding, infrastructure damage, …) plug in
behind it without touching the engine.

---

## 1. The core idea: everything is a producer

A **producer** is anything that can materialise a cube variable. There
are two kinds, and the engine treats them identically:

| Kind          | Reads                | Writes                | Example |
|---------------|----------------------|-----------------------|---------|
| **Data driver** | external bytes      | a cube variable       | ERA5 wind, LANDFIRE fuel, DEM, KML thermal pulse |
| **Model**       | *other cube variables* | a cube variable    | WRF-SFIRE: ignition_t0 + nfuel_cat + dem → arrival_s |

Both implement the same contract (`engine/contracts.py`):

```python
produces : tuple[VarSpec, ...]      # variables it can make
requires : tuple[VarSpec, ...]      # variables it needs
run(cube, request) -> {var: version}
```

Because they are uniform, **a model that needs another model's output
just lists that output in its `requires`.** The engine resolves the rest.

---

## 2. How the cascade resolves itself

Two engine components do all the dependency work — neither knows
anything about fire, smoke, or floods:

### `Pipeline.from_targets` (`engine/pipeline.py`)
Given target variables, it walks `requires → produces` **backwards**
through the registry and emits a topologically-ordered DAG.

### `DataAdapter.resolve` (`engine/data_adapter.py`)
The "where does data come from?" engine. For each input a model needs:

1. Is it already in the **cube** (data catalog) at the required
   resolution? → use it.
2. Otherwise, find the **producer** registered for that variable —
   *data driver or another model, it doesn't care* — run it, re-check.
3. Still missing & required → raise; optional → skip.

That step-2 indifference is the whole cascade. Today:

```
KML ──ThermalDriver──▶ ignition_t0 ─┐
LANDFIRE ─────────────▶ nfuel_cat ──┼──▶ WRF-SFIRE ──▶ arrival_s, fire_area
DEM ──────────────────▶ dem ────────┘                   (model 1)
ERA5 ─────────────────▶ wind_* ─────▶ (met_em / sounding)
```

Add **model 2** that consumes the fire output:

```
                         WRF-SFIRE ──▶ arrival_s ──▶ SmokeModel ──▶ pm25_conc
                          (model 1)                   (model 2)
```

You request `pm25_conc`; `Pipeline.from_targets` sees `SmokeModel`
requires `arrival_s`, which `WRF-SFIRE` produces, which requires
`ignition_t0`… and builds the full chain. **No engine code changes.**

---

## 3. Plugging in a model

Two steps.

### a. Write a `ModelAdapter` (`engine/model_adapter.py`)
Three hooks — stage inputs to disk, run the binary, parse outputs back:

```python
class SmokeModel(ModelAdapter):
    name = "smoke_dispersion"
    data_adapter = DataAdapter([
        DataNeed("arrival_s",     kind="static"),   # ← WRF-SFIRE output
        DataNeed("fire_area",     kind="static"),
        DataNeed("wind_speed_ms", kind="time"),
    ])
    produces = (VarSpec("pm25_conc", kind="time", units="ug/m^3"),)

    def stage_inputs(self, grid, inputs, request, stage): ...
    def run_model(self, stage, request): ...
    def parse_outputs(self, output_path, grid): ...
```

### b. Register it in the catalog (`models/catalog.py`)
```python
cat.add_model("smoke", build_smoke_dispersion)   # one line
```

That's the entire integration surface. The engine handles dependency
resolution, resolution-aware skip, dirty propagation, retries, parallel
execution, and lineage for free.

---

## 4. Infrastructure independence

Nothing model-specific is HPC-specific. The split:

| Concern            | Where                       | What it does |
|--------------------|-----------------------------|--------------|
| HPC detection      | `hpc/profiles.py`           | Auto-detects CHPC / Stampede3 / Derecho / Frontier / generic SLURM from Lmod + hostname; supplies modules, MPI launcher, SLURM defaults, WRF configure answers |
| Job submission     | `hpc/slurm.py`              | Generates + submits SLURM batch scripts, polls `sacct` |
| Module loading     | `hpc/profiles.load_modules` | `module load` through a bash subshell, env propagated back |

The model adapter never names a cluster. The **catalog** builds the MPI
launch line from the detected profile:

```python
wrf_cmd = profile.mpi_cmd(ctx.wrf_bin, nranks)
#  CHPC      -> mpirun -np 56 ./wrf.exe
#  Stampede3 -> ibrun -np 96 ./wrf.exe
#  Frontier  -> srun --ntasks=64 ./wrf.exe
```

Same code, any machine. To add a cluster: one `HPCProfile` entry.

---

## 5. The data catalog (cube)

The **cube** (`cube/`) is the single source of truth — Zarr arrays +
DuckDB spatial/temporal index. Every producer writes here at the
variable's **native resolution**; consumers declare a
`max_native_res_m` and the engine re-runs upstream if cached data is too
coarse. Content-addressable caching + lineage (git SHA, config hash,
input hashes) make runs reproducible and incremental.

---

## 6. Running it

```bash
# Inspect the DAG for any target set — nothing executes
python scripts/run_cascade.py --dry-run --targets arrival_s,fire_area

# Wildfire model (model 1), local node, auto-detected HPC profile
python scripts/run_cascade.py \
    --kml Dallas.kml --center -96.809 32.780 --radius-km 50 \
    --pixel-m 900 --fire-mesh-ratio 10 \
    --start 2019-09-04T12:00 --sim-hours 24 \
    --targets arrival_s,fire_area

# Real-data path: build met_em via WPS first, then run real.exe
python scripts/run_cascade.py --real --build-met-em \
    --domain-km 600 --dx-m 3000 --targets arrival_s,fire_area

# A future downstream model — WRF-SFIRE runs automatically in front
python scripts/run_cascade.py --targets pm25_conc
```

`--targets` drives everything. The same script runs one model or a ten-
model cascade; the only difference is which variables you ask for.

---

## 7. Component map

```
NASA_Project/
├── engine/                 # model-agnostic orchestration (DO NOT specialise)
│   ├── contracts.py        #   ProducerV2, VarSpec, MergePolicy
│   ├── data_adapter.py     #   DataAdapter.resolve — the cascade resolver
│   ├── pipeline.py         #   Pipeline.from_targets — DAG builder
│   ├── model_adapter.py    #   ModelAdapter base (3-hook plug-in)
│   └── scheduler.py        #   PipelineRunner — executes the DAG
├── cube/                   # data catalog (Zarr + DuckDB)
├── drivers/                # data drivers (external bytes -> cube var)
│   ├── thermal.py          #   KML thermal pulse  -> ignition_t0
│   ├── landfire_fbfm13.py  #   LANDFIRE           -> nfuel_cat
│   ├── dem.py              #   USGS/Copernicus    -> dem
│   ├── era5_wind.py        #   ARCO-ERA5          -> wind_*
│   └── wps_meteo.py        #   ERA5 GRIB -> WPS    -> met_em  (real path)
├── models/                 # models (cube vars -> cube var)
│   ├── wrf_sfire_adapter.py#   MODEL 1 (wildfire)
│   ├── catalog.py          #   plug-a-model registry  <-- add models here
│   └── external_model_template.py
├── hpc/                    # infrastructure independence
│   ├── profiles.py         #   detect cluster, modules, MPI launcher
│   └── slurm.py            #   SLURM script gen + submit
├── templates/              # corrected WRF inputs (ifire=1, full SFIRE)
│   ├── namelist.input_ignite_from_tign_in
│   ├── namelist.fire
│   ├── input_sounding
│   └── Vtable.ERA5
└── scripts/
    └── run_cascade.py      # generic targets -> DAG -> any HPC runner
```

> The compiled `WRF/WRF-SFIRE/` stack outside this project is consumed
> read-only via `--install-root`; the cascade never modifies it.

---

## 8. Key corrections baked into the templates

The templates fix the issues found while running WRF-SFIRE by hand:

| Setting | Why |
|---------|-----|
| `ifire = 1` | Selects the full SFIRE physics (`module_fr_sfire_phys`), whose namelist accepts the extended fuel parameters (`ffw`, `adjr0`, …). `ifire = 2` is the *older* module and rejects them. |
| Full `namelist.fire` | Works under `ifire = 1`; the stripped version was only needed because `ifire = 2` used the basic module. |
| `fire_atm_feedback = 1.0` | Real fire–atmosphere coupling (pyroconvection). Stable here because ignition is gridded via `TIGN_IN`, not a heat-shock point source. |
| `TIGN_IN` ignition | The whole impact footprint is pre-ignited from the `ignition_t0` field (KML fluence → time), the physically correct asteroid scenario — not a single point with a huge radius. |
| `restart_interval = 1440` | Daily checkpoints, so a crash on day N doesn't lose the run. |
| ERA5 `geopotential` + `mean_sea_level_pressure` | Needed for `SOILHGT`/`SLP`; their absence caused the `p sfc computation` failures. |
