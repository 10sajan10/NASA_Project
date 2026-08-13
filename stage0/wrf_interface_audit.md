# WRF interface and failure audit

This audit separates three scientifically different components that the legacy
path can otherwise blur together.

## 1. Idealized WRF-SFIRE

Purpose: smallest reference fire-atmosphere run, without real meteorological
boundary data.

Inputs:

- `ignition_t0`, `nfuel_cat`, and `dem` from the cube;
- `namelist.input`, `namelist.fire`, and `input_sounding` templates;
- optional `wind_speed_ms` and `wind_dir_deg`, used only to patch the sounding;
- ideal/WRF executables and runtime tables.

Execution: stage inputs, patch ignition/fuel/terrain/sounding, run `ideal.exe`
when configured, then run `wrf.exe`.

Outputs: `TIGN_G -> arrival_s` and `FIRE_AREA -> fire_area`, plus opt-in
diagnostics/smoke fields. Process exit success is insufficient: ignition, fire
growth, finite/plausible fields, grid mapping, and time/domain consistency must
all pass.

## 2. Real-data WRF-SFIRE

Purpose: coupled fire run initialized and forced by WPS meteorology.

Inputs add:

- a complete time/domain collection of `met_em.d0*` files;
- WPS/geogrid/ungrib/metgrid products and tables;
- `real.exe`, which creates `wrfinput_d0*` and `wrfbdy_d01`;
- boundary interval, nesting, restart, and execution profile.

Cube wind is not the atmospheric initial/boundary condition in this mode. The
adapter explicitly obtains it from `met_em`. Therefore a direct ERA5 10-m wind
cube and WPS meteorology are not interchangeable contracts.

## 3. Atmospheric WRF as a wind producer

Purpose: a competing producer for a wind requirement at a requested grid,
height/level, extent, and time window.

This is **not yet a conforming standalone component**. Before it enters resolver
competition it needs a golden downscaling run with:

- exact upstream boundary asset manifest;
- declared horizontal grid/CRS, cadence/time semantics, vertical coordinate or
  reference height, units, ensemble/member semantics, and coverage;
- extraction of the requested wind descriptor from WRF output;
- independent validation/evidence calculation;
- explicit compute resource and execution profile.

## Reproduced historical failures

No new expensive WRF job was launched in Stage 0. Existing retained logs are
enough to reproduce and classify these defects:

| Class | Evidence | Interpretation |
|---|---|---|
| Domain/cube mapping | `logs/20260708_005323_cascade_20190904T120000_targets.json` | `arrival_s` is `(253,253)` while the cube is `(1001,1001)`. Current block reduction/slicing does not define placement or reprojection onto the cube. |
| Same mapping defect | `logs/20260706_230418_cascade_20190904T120000_targets.json` and `logs/20260626_045824_cascade_20190904T120000_targets.json` | The mismatch is recurrent, not a single corrupt run. |
| Resource/process loss | `logs/20260706_230216_cascade_20190904T120000_targets.json` | `wrf.exe` under 56-rank `mpirun` died with SIGKILL; the legacy manifest cannot distinguish OOM, scheduler termination, or external kill. |
| MPI/runtime warnings | corresponding July 2026 text logs | OpenFabrics warnings are present and require a certified execution profile rather than ad-hoc launcher selection. |

## Gate conclusion

- The three modes have separate contracts.
- The ideal and real-data adapter paths exist, but no run is promoted as a
  trusted golden scientific fixture in Stage 0.
- The 253/1001 mapping defect is a blocker for WRF output publication.
- Atmospheric WRF cannot compete as a wind producer until its standalone
  reference gate passes.
- WRF reference work may proceed on any authorized/certified provider; SLURM is
  required only when that execution profile requires queued or multi-node work.
