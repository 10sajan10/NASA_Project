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
- Models for LFMC, dead-fuel moisture, drought, and Rothermel/Dijkstra fire
  spread.
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
  -> fire spread
```

For ERA5-driven future weather, provide a local ERA5 NetCDF/Zarr:

```bash
python run.py --engine resolver --weather era5 --era5-source /path/to/era5.zarr
```

For population exposure, provide a population-count raster:

```bash
python run.py --engine resolver --population-raster /path/to/population.tif
```
