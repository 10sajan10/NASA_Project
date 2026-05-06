# NASA Wildfire Scenario Prototype

This project is a geospatial wildfire-spread prototype for a future
planetary-defense thermal ignition scenario. It builds a central simulation
cube, writes source data and model outputs into that cube, and runs downstream
models by reading required variables from storage rather than calling other
models directly.

The current implementation uses:

- Zarr arrays for per-variable raster storage.
- DuckDB for the cube catalog.
- A fixed UTM simulation grid per scenario.
- Data adapters for thermal ignition, LANDFIRE fuels, DEM, Landsat/Sentinel
  indices, CMIP6/synthetic/ERA5 weather, and population rasters.
- Models for LFMC, dead-fuel moisture, drought, fuel-dependent ignition/spread
  thresholds, and Rothermel/Dijkstra fire spread.
- A dependency resolver that lets a model request variables and automatically
  runs the data/model chain needed to create missing cube layers.

Large generated data products, local virtual environments, and LANDFIRE raster
downloads are intentionally ignored by Git.

## Resolver Engine

The resolver engine is the preferred path for new work:

```bash
python run.py \
  --engine resolver \
  --satellite landsat \
  --weather synthetic \
  --scenario-date 2036-09-15 \
  --days 3
```

The fire model requests its final variables from the cube. Missing upstream
variables are produced automatically by registered producers:

```text
fire
  -> LANDFIRE fuels
  -> DEM + slope/aspect
  -> thermal ignition and burnable mask
  -> Landsat historical NDVI/NDWI/NBR
  -> satellite-index trend prediction
  -> LFMC
  -> weather
  -> dead fuel moisture
  -> KBDI
  -> fuel thresholds / hard barriers / urban resistance
  -> fire spread
```

The threshold layer separates surface behavior into:

```text
hard barriers: snow/ice, maintained agriculture, water, bare ground
wildland: grass, shrub, timber, slash via Rothermel spread
urban/WUI: high ignition/spread threshold with slower spread
```

For population exposure, provide a population-count raster:

```bash
python run.py --engine resolver --population-raster /path/to/population.tif
```

## Cube Snapshots

The resolver already reuses any variable present in the active cube. For
example, if `ndvi` exists in `data/cube/ndvi.zarr`, a later fire run will read
that layer instead of re-running the satellite producer.

Named snapshots make that reuse explicit:

```bash
python run.py --save-snapshot dallas_inputs_v1
```

Restore a snapshot into a run root:

```bash
python run.py \
  --from-snapshot dallas_inputs_v1 \
  --overwrite-root \
  --recompute-fire
```

`--recompute-fire` removes only downstream fire outputs such as `arrival_s`,
`fire`, `R_head`, and population exposure. Upstream inputs such as fuels, DEM,
NDVI/NDWI/NBR, LFMC, weather, and threshold layers remain available for reuse.
