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
- Data drivers for thermal ignition, LANDFIRE fuels, DEM, Sentinel-derived
  indices, CMIP6 or synthetic weather.
- Models for LFMC, dead-fuel moisture, drought, and Rothermel/Dijkstra fire
  spread.

Large generated data products, local virtual environments, and LANDFIRE raster
downloads are intentionally ignored by Git.
