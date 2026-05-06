# Wildfire Spread Prototype — Full Project Context

## What this is
Post-impact wildfire spread simulation from a 2023 PDC (Planetary Defense Conference)
asteroid scenario. Not a normal wildfire — ignition is a simultaneous annular ring
~42km radius at 8 US cities from a thermal damage KMZ file.

This is a FUTURE-SETTING simulation. Most inputs cannot be observed — they must be
predicted or extrapolated from historical data. Each input has its own model.

---

## Ignition geometry (from KMZ)

- Source: ThermalDamageMaps_2023PDC-Epoch1.kmz
- t=0 perimeter = "Critical Burn (clothing)" ring (~42km radius)
- Inner dead zone = "Unsurvivable Burn (structures)" ring (~35km) — no fuel, suppressed
- Fire spreads OUTWARD ONLY from the ring
- US cities in KMZ: Dallas TX, San Angelo TX, Little Rock AR, Memphis TN,
  Huntsville AL, Nashville TN, Richmond VA, Salisbury MD

---

## System design — model cascade

Four model layers run in sequence. Each writes outputs to the cube.
The fire spread model reads from the cube — it never calls other models directly.

```
LAYER 0 — Static ingest (run once)
  DEM centroids          → cube: dem(x,y)            native_res=10m   static
  LANDFIRE centroids     → cube: fbfm40(x,y)          native_res=30m   static
  Ignition ring          → cube: ignition(x,y)        vector           t=0

LAYER 1 — Vegetation state models (predict future input state)
  NDVIModel              → cube: ndvi(x,y,t)          native_res=10m   per month
  NDWIModel              → cube: ndwi(x,y,t)          native_res=10m   per month
  LFMCModel              → cube: lfmc(x,y,t)          native_res=10m   per month

LAYER 2 — Weather models (predict future atmospheric state)
  WindModel              → cube: wind_u(x,y,t)        native_res=3000m hourly
                           cube: wind_v(x,y,t)
  MoistureModel          → cube: rh(x,y,t)            native_res=3000m hourly
                           cube: temp_c(x,y,t)
  DroughtModel           → cube: kbdi(x,y,t)          native_res=3000m daily
  DeadFuelModel          → cube: dfm_1hr(x,y,t)       derived          hourly
                           cube: dfm_10hr(x,y,t)
                           cube: dfm_100hr(x,y,t)

LAYER 3 — Fire spread model
  FireAdapter/elmfire    → cube: fire(x,y,t) {0,1}   native_res=100m  hourly
                           cube: arrive_sec(x,y)
                           cube: burn_prob(x,y)        from ensemble
```

---

## Per-model implementation plan

### NDVIModel
Purpose: predict vegetation greenness at scenario date (future, no satellite available)
Method:
  - Collect all Sentinel-2 scenes for bbox, cloud < 20%, years 2016-2024
  - For each pixel: fit linear trend NDVI(t) = slope * t + intercept
  - Extrapolate to scenario date → predicted NDVI per cell
  - Also compute monthly climatological median as fallback
  - Write per-cell predicted NDVI to cube
Input:  Sentinel-2 archive via Microsoft Planetary Computer (free, no login)
Output: ndvi(x,y,t=scenario_date), native_res=10m, v0

### NDWIModel
Purpose: predict canopy water content (proxy for live fuel moisture)
Method:
  - Same Sentinel-2 archive as NDVI
  - NDWI = (NIR - SWIR1) / (NIR + SWIR1)  [Gao 1996]
  - Same trend extrapolation approach as NDVI
  - Monthly climatological median as fallback
Input:  Sentinel-2 B08 (NIR), B11 (SWIR1)
Output: ndwi(x,y,t=scenario_date), native_res=10m, v0

### LFMCModel
Purpose: predict live fuel moisture content (%)
Method:
  - Yebra et al. (2013) regression: LFMC = 125 + 288 * NDWI
  - Applied per cell to predicted NDWI field
  - No separate data download — derived from NDWIModel output
Input:  ndwi(x,y,t) from cube
Output: lfmc(x,y,t), native_res=10m, v0

### WindModel
Purpose: provide wind field for full 30-day simulation horizon
Three-horizon strategy:
  - 0-48h:    HRRR deterministic (3km, hourly) via herbie
  - 3-16 days: GFS ensemble 21 members (13km, 6-hourly) via herbie
  - 16-30 days: climatological analog sampling
                find historical months matching scenario month + ENSO state
                sample N wind fields, store as ensemble members in cube
Input:  herbie (HRRR/GFS), ERA5 climatology for analog pool
Output: wind_u(x,y,t), wind_v(x,y,t), native_res varies by horizon

### MoistureModel
Purpose: RH and temperature for full 30-day horizon (same three horizons as wind)
Method: identical horizon strategy to WindModel
Input:  herbie HRRR/GFS RH and TMP fields
Output: rh(x,y,t), temp_c(x,y,t)

### DeadFuelModel
Purpose: predict 1-hr, 10-hr, 100-hr dead fuel moisture (%)
Method:
  - Nelson (1984) EMC equation applied per cell per timestep:
      if rh < 10:  emc = 0.03229 + 0.281073*rh - 0.000578*rh*temp_c
      if rh < 50:  emc = 2.22749 + 0.160107*rh - 0.014784*temp_c
      else:        emc = 21.0606 + 0.005565*rh^2 - 0.00035*rh*temp_c - 0.483199*rh
  - 1-hr = EMC (equilibrium, fast response)
  - 10-hr = 1-hr * 1.35  (lag multiplier)
  - 100-hr = 1-hr * 1.75
  No separate data download — derived from MoistureModel output
Input:  rh(x,y,t), temp_c(x,y,t) from cube
Output: dfm_1hr(x,y,t), dfm_10hr(x,y,t), dfm_100hr(x,y,t)

### DroughtModel
Purpose: KBDI (Keetch-Byram Drought Index, 0-800) as deep fuel moisture proxy
Method:
  - Forward integrate KBDI daily using HRRR precip + temp
  - KBDI 0 = saturated, 800 = extreme drought
  - Use pre-scenario historical KBDI as initial condition
  - Pull daily HRRR APCP (accumulated precip) for forcing
Input:  HRRR APCP, temp_c from cube
Output: kbdi(x,y,t), native_res=3000m, daily cadence

### FireAdapter (elmfire)
Purpose: simulate fire spread from ignition ring outward for 30 days
Method:
  - Reads all required variables from cube via nearest-centroid query
  - Builds elmfire input files (namelist + raster grids)
  - Runs elmfire_cuda
  - Reads arrival time raster output
  - Writes fire(x,y,t) and arrive_sec(x,y) back to cube as v1
Runs as ensemble: 100 members, perturb wind ±15%, moisture ±20%
After ensemble: compute burn_prob(x,y) = fraction of members that burned
Input (from cube):
  wind_u, wind_v, rh, temp_c, dfm_1hr, dfm_10hr, dfm_100hr,
  lfmc, fbfm40, dem, slope, aspect, cc, ch, cbd, cbh, kbdi
Output (to cube):
  fire(x,y,t) = {0,1}  100m resolution  hourly timesteps  v1
  arrive_sec(x,y)       arrival time in seconds since ignition
  burn_prob(x,y)        0.0-1.0 from ensemble

---

## Spatiotemporal cube — PostGIS centroid store

Single table. Everything — inputs and outputs — lives here.

```sql
CREATE TABLE cube (
    id           BIGSERIAL PRIMARY KEY,
    variable     TEXT,
    x            FLOAT,        -- centroid longitude
    y            FLOAT,        -- centroid latitude
    z            FLOAT,        -- elevation (optional)
    t            TIMESTAMPTZ,  -- NULL for static layers
    value        FLOAT,
    native_res_m INT,           -- 10, 30, 3000, 100
    version      SMALLINT,     -- 0=observed/predicted, 1=model output
    source       TEXT,         -- 'LANDFIRE_2022', 'HRRR', 'elmfire_v2', etc.
    uncertainty  FLOAT
);
CREATE INDEX cube_spatial ON cube USING GIST (ST_MakePoint(x, y));
CREATE INDEX cube_var_t   ON cube (variable, t);
```

Resolution reconciliation: nearest-centroid lookup only.
Distance to centroid is the uncertainty signal. No resampling at ingest.

### Query pattern all models use
```python
def query(variable, x, y, t, max_search_m=10000):
    # returns nearest centroid value + distance as uncertainty
```

### Write pattern all models use
```python
def write(variable, centroids_df, source, version=0):
    # centroids_df has columns: x, y, t, value, native_res_m, uncertainty
```

---

## Downscaling policy (not a research problem)

Adapter author declares method. Framework executes it.
- NEAREST   → categorical variables (fbfm40, land cover)
- BILINEAR  → continuous smooth (wind, temp, rh)
- BICUBIC   → terrain (dem, slope)
Uncertainty flagged when distance > native_res * 0.5.

---

## Data sources (all free, all public)

| Layer | Source | Access |
|---|---|---|
| DEM | USGS 3DEP | `pip install elevation` |
| Fuels | LANDFIRE LFPS API | requests to lfps.usgs.gov |
| NDVI/NDWI | Sentinel-2 L2A | Planetary Computer (no login) |
| Wind/RH | HRRR + GFS | `pip install herbie-data` |
| Live FMC | Derived from NDWI | Yebra regression |
| Dead FMC | Derived from RH/temp | Nelson EMC |
| Drought | HRRR APCP | same herbie fetch as wind |

---

## Repo layout

```
CLAUDE.md                  ← this file
cube/
  schema.sql               ← PostGIS table + indexes
  db.py                    ← connect(), query(), write()
  ingest.py                ← raster_to_centroids(tif, variable, res)
drivers/
  dem.py                   ← USGS 3DEP download
  landfire.py              ← LANDFIRE LFPS API
  hrrr.py                  ← herbie wind/RH/precip, 3-horizon strategy
  sentinel.py              ← Planetary Computer NDVI/NDWI
models/
  ndvi_model.py            ← trend extrapolation per cell
  ndwi_model.py            ← same pattern as ndvi_model
  lfmc_model.py            ← Yebra regression on NDWI
  dead_fuel_model.py       ← Nelson EMC per cell per timestep
  drought_model.py         ← KBDI forward integration
  wind_model.py            ← 3-horizon wind strategy
adapters/
  base.py                  ← ModelAdapter ABC: required_inputs, produces, execute()
  fire_adapter.py          ← wraps elmfire GPU, reads cube, writes back
run.py                     ← main: parse KMZ → ingest → run model cascade
data/
  ThermalDamageMaps_2023PDC-Epoch1.kmz
```

---

## Execution order

```
python run.py --city "Dallas TX USA" --scenario-date 2023-10-20 --days 30

1. parse KMZ          → extract ignition ring + dead zone for city
2. ingest static      → DEM + LANDFIRE centroids → cube
3. NDVIModel          → historical Sentinel → trend → predicted NDVI → cube
4. NDWIModel          → same archive → predicted NDWI → cube
5. LFMCModel          → NDWI from cube → LFMC → cube
6. WindModel          → HRRR/GFS/analog → wind_u, wind_v → cube
7. MoistureModel      → HRRR/GFS → rh, temp_c → cube
8. DeadFuelModel      → rh+temp from cube → dfm_1hr/10hr/100hr → cube
9. DroughtModel       → HRRR APCP → KBDI → cube
10. FireAdapter x100  → all variables from cube → elmfire GPU → fire(x,y,t) → cube
11. post-process      → burn_prob(x,y), arrival maps, perimeter GeoJSONs
```

---

## Build order for Claude Code

Start here, in this order:
1. `cube/schema.sql` — table definition
2. `cube/db.py` — query() and write() only, nothing else
3. `cube/ingest.py` — raster_to_centroids() for one file
4. `drivers/dem.py` — download and ingest DEM for one city bbox
5. `models/ndvi_model.py` — trend extrapolation (testable independently)
6. `adapters/base.py` — ModelAdapter ABC
7. `adapters/fire_adapter.py` — stub first, wire elmfire later
8. `run.py` — wire everything for one city, one ensemble member
