# System Architecture — WRF-SFIRE as the worked example

A scalable, infrastructure-independent system for predicting the
**cascading environmental effects of an asteroid impact**. Every model
is a pluggable producer; the data catalog is the single source of truth;
the engine resolves a model's data needs — running upstream models when
one model depends on another. WRF-SFIRE (wildfire) is **model 1**.

---

## 1. The big picture (layers)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  USER                                                                      │
│     configs/wildfire_scenario.yaml      scripts/run_cascade.py --targets   │
│     (extent_km, nests, days, smoke)     (what to produce drives everything)│
└───────────────────────────────┬────────────────────────────────────────────┘
                                 │
┌────────────────────────────────▼───────────────────────────────────────────┐
│  ORCHESTRATION ENGINE         (model-agnostic — never edited per model)     │
│                                                                             │
│   Pipeline.from_targets ──► DAG over producers (walks requires→produces)    │
│   DataAdapter.resolve   ──► "where does data come from?" (cube? or run it)  │
│   PipelineRunner        ──► executes DAG on a Backend (serial/thread/proc)  │
│   contracts: produces / requires / merge_policy / capabilities              │
└───────┬───────────────────────────────────────────────────┬─────────────────┘
        │                                                   │
┌────────▼─────────────┐                          ┌──────────▼─────────────────┐
│  DATA CATALOG (cube) │                          │  PRODUCERS (plug-ins)       │
│  Zarr arrays +       │  ◄── read / write ──►    │                             │
│  DuckDB index        │   every var at its       │  data drivers   +  models   │
│  native-res, lineage │   native resolution      │  (external bytes)  (compute)│
└──────────────────────┘                          └──────────┬──────────────────┘
                                                            │
┌────────────────────────────────────────────────────────────▼─────────────────┐
│  INFRASTRUCTURE INDEPENDENCE  (hpc/)                                          │
│   detect_profile() ─ chpc_utah | stampede3 | derecho | frontier | generic    │
│   profile.mpi_cmd() ─ mpirun -np 56 │ ibrun │ srun --ntasks   (same code)     │
│   SlurmJob ─ generate + submit batch script,  load_modules()                 │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. WRF-SFIRE as a producer — its contract

A model declares **what it needs** and **what it makes**. That's the
entire integration surface the engine sees.

```
                        ┌───────────────────────────────────┐
   requires (inputs)    │      WRFSFireAdapter  (model 1)    │   produces (outputs)
                        │      name = "wrf_sfire_asteroid"   │
   ignition_t0  ───────►│                                    │──►  arrival_s
   nfuel_cat    ───────►│   stage_inputs → run_model →       │──►  fire_area
   dem          ───────►│   parse_outputs                    │──►  ros_max*
   wind_speed_ms ──────►│                                    │──►  fire_intensity*
   wind_dir_deg ───────►│   (* opt-in: extra_outputs)        │──►  fuel_consumed*
                        │   († opt-in: smoke_outputs)        │──►  pm25_surface†
                        └───────────────────────────────────┘──►  smoke_tracer†
```

The engine reads only `requires` / `produces` / `merge_policy`. It never
looks inside the model. Swap WRF-SFIRE for another fire model and
nothing else changes.

---

## 3. Where each input comes from (the cascade resolver)

`DataAdapter.resolve` walks each input. If the cube already has it →
use it. Otherwise find the registered producer — *data driver or model,
it doesn't care* — run it, re-check.

```
   WRF-SFIRE needs:        resolved by:                  produces into cube:
   ─────────────────       ───────────────────────       ──────────────────
   ignition_t0      ◄───   ThermalDriver  (Dallas.kml)  ─►  ignition_t0
   nfuel_cat        ◄───   LandfireFBFM13Driver         ─►  nfuel_cat
   dem              ◄───   DEMDriver (USGS 3DEP)         ─►  dem
   wind_speed_ms    ◄───   ERA5WindDriver (ARCO-ERA5)   ─►  wind_*
   wind_dir_deg     ◄───   ERA5WindDriver                  (+ WPSMeteoDriver
                                                            ─► met_em for real.exe)
```

DAG the engine builds from `--targets arrival_s,fire_area`:

```
   ThermalDriver ──┐
   LandfireDriver ─┤
   DEMDriver ──────┼──►  WRF-SFIRE  ──►  arrival_s, fire_area
   ERA5WindDriver ─┘     (model 1)
   (WPSMeteoDriver ─► met_em ─┘  when use_real)
```

---

## 4. Inside the model run (the three hooks)

```
        cube vars (fetched for you)
              │
   ┌──────────▼───────────┐   stage_inputs(grid, inputs, request, stage/)
   │  STAGE               │   • generate namelist.input  (NamelistBuilder:
   │  build a WRF run dir │       nested domains d01/d02/d03, &chem if smoke)
   │                      │   • copy namelist.fire (full SFIRE, ifire=1)
   │                      │   • real.exe ← met_em   OR   ideal.exe ← sounding
   │                      │   • inject TIGN_IN / NFUEL_CAT / ZSF into
   │                      │     wrfinput_d0N.nc   (gridded asteroid ignition)
   └──────────┬───────────┘
              │
   ┌──────────▼───────────┐   run_model(stage, request)
   │  RUN                 │   profile.mpi_cmd(wrf.exe, np)
   │  mpirun -np 56       │     CHPC: mpirun · Stampede: ibrun · Frontier: srun
   │  ./wrf.exe           │   (or submit as a SLURM batch job)
   └──────────┬───────────┘
              │  wrfout_d0N_*  (fire mesh: TIGN_G, FIRE_AREA, ROS, FGRNHFX …)
   ┌──────────▼───────────┐   parse_outputs(output_path, grid)
   │  PARSE               │   • TIGN_G  → arrival_s   (block-reduce fire→cube)
   │  wrfout → cube vars  │   • FIRE_AREA→ fire_area
   │                      │   • ROS/FGRNHFX/FUEL_FRAC → ros_max/intensity/…
   └──────────┬───────────┘   • PM2_5_DRY → pm25_surface  (if chem build)
              │
              ▼
        DATA CATALOG (cube)  — written at native resolution, with lineage
```

---

## 5. Configuration → namelists (nesting & smoke)

```
  configs/wildfire_scenario.yaml
     domain.extent_km: 1000
     domain.resolutions_m: [9000, 3000, 1000]   chem.smoke: true
            │
            ▼   load_scenario_yaml()
     WRFScenario  (solves WRF index arithmetic for explicit nests)
            │
   ┌────────┴─────────┐
   ▼                  ▼
 NamelistBuilder    WPSNamelistBuilder
   │                  │
   ▼                  ▼
 namelist.input     namelist.wps
   max_dom = 3        max_dom = 3
   e_we = 112,169,253     (geogrid/ungrib/metgrid → met_em on d01)
   dx   = 9000,3000,1000
   sr_x = 0,0,10      ← fire mesh on innermost only (100 m)
   ifire= 0,0,1       ← SFIRE physics on fire domain
   &chem chem_opt=17  ← smoke tracers (needs WRF_CHEM=1 binary)
```

---

## 6. Adding model 2 — the cascade extends itself

A downstream model just declares a WRF-SFIRE output in its `requires`.
Ask for *its* target and the engine runs WRF-SFIRE first, automatically.

```
   WRF-SFIRE ──► arrival_s ──┐
                fire_area ───┼──►  SmokeDispersion ──►  pm25_concentration
   ERA5Wind  ──► wind_* ─────┘        (model 2)

   models/catalog.py:
       cat.add_model("smoke", build_smoke)     # ← one line, no engine change

   run:
       run_cascade.py --targets pm25_concentration
       #   engine sees model 2 needs arrival_s → runs WRF-SFIRE → runs model 2
```

Generalises to a chain: flooding, infrastructure damage, air-quality,
each consuming upstream outputs. The engine, catalog pattern, cube, and
HPC layer never change — only new `ModelAdapter`s get registered.

---

## 7. Component → file map

```
 USER          configs/wildfire_scenario.yaml      scripts/run_cascade.py
 ENGINE        engine/{contracts,pipeline,data_adapter,model_adapter,scheduler}.py
 CATALOG       models/catalog.py        (plug-a-model registry — add models here)
 CONFIG→NML    models/wrf_config.py     (WRFScenario + NamelistBuilder + WPS)
 MODEL 1       models/wrf_sfire_adapter.py
 DATA DRIVERS  drivers/{thermal,landfire_fbfm13,dem,era5_wind,wps_meteo}.py
 CATALOG/STORE cube/{store,catalog,grid}.py        (Zarr + DuckDB)
 INFRA         hpc/{profiles,slurm}.py
 TEMPLATES     templates/{namelist.input_ignite_from_tign_in,namelist.fire,Vtable.ERA5}
 BOOTSTRAP     models/wrf_sfire_bootstrap.py   (clone+compile MPI/chem WRF stack)
```
