# Stage 9B — WRF integration blocked; placement prerequisites implemented

Status: **the WRF execution/integration stage is blocked at its entry gates.**
This folder contains independently useful prerequisites that were buildable
without launching WRF or inventing scientific evidence: typed publication
placement, whole-plan preflight, verified native WRF georeferencing, and
variable-specific resampling rules. None of them is a completed WRF runtime
path.

## Why WRF execution did not start

The roadmap opens Stage 9B with: *"Begin after Stage 6, WRF reference lane R1
(and R2 for real-data mode), and one provider certified for the selected
ExecutionProfile."* All three conditions are unmet:

| Gate | State |
|---|---|
| R1 golden fixture | none promoted — `stage0/wrf_interface_audit.md`: *"no run is promoted as a trusted golden scientific fixture in Stage 0"* |
| R2 / R2A real-data lane | not built |
| one certified provider | Stage 9A reaches the controller only through a hermetic fake scheduler; no real site is certified and no MPI gang task exists |

The same audit is blunt about the consequence: *"Atmospheric WRF cannot compete
as a wind producer until its standalone reference gate passes"* and *"The
253/1001 mapping defect is a blocker for WRF output publication."*

Starting the WRF runtime path on top of that would mean using WRF as the first
runtime correctness test — the one thing the roadmap explicitly forbids.

## What was built instead

The 253/1001 defect is a real, recorded, recurrent blocker, and it needs no
cluster, no WRF binary, and no fabricated fixture to fix. From
`logs/20260708_005323_cascade_20190904T120000_targets.json`:

```json
{"name": "wrf_sfire_asteroid", "status": "error",
 "elapsed_s": 174849.76274463162,
 "error": "ValueError: arrival_s: array shape (253, 253) != grid (1001, 1001)",
 "attempts": 1, "dead_letter": true}
```

**48.6 hours of compute, dead-lettered on a shape comparison.** Three retained
logs show the same defect, so this is a design fault, not a corrupt run.

Two things were wrong, and only one of them is about shapes.

### The check ran at the wrong time

`cube/store.py` compares an array's shape to the cube's shape at write time.
That is the *last* thing a run does. But whether one grid's array can be placed
onto another is a property of the two **descriptors** — it does not depend on a
single array value, so it is knowable before any core-hour is spent.

`contracts/placement.py` is pure and allocates nothing.
`cube/preflight.py` runs it across a whole publication plan up front. This is a
callable launch gate, but the retained legacy cascade and the not-yet-built
target-to-execution service do not invoke it automatically.

### The check asked the wrong question

"Shape mismatch" is the symptom. The real question is whether a *declared
relationship* exists between the two grids. Shape alone answers it in neither
direction:

- An aligned, whole-block 100 m mesh and a same-CRS 900 m target have different
  shapes and can place exactly — 900/100 = 9. The ratio alone is insufficient:
  CRS, axis direction, leading-edge alignment, containment, and complete blocks
  must also agree.
- Two 1001×1001 grids in different CRSs have identical shapes and do **not**
  place at all.

So `assess_placement` returns a typed verdict naming the relationship:

| Verdict | Meaning |
|---|---|
| `EXACT_MATCH` | same grid, cell for cell |
| `INTEGER_REFINEMENT` | source is finer by an aligned integer factor, spanning whole blocks |
| `INTEGER_COARSENING` | source is coarser by an aligned integer factor |
| `REQUIRES_DECLARED_RESAMPLING` | georeferenced, but needs interpolation |
| `UNDEFINED_NO_GEOREFERENCE` | the recorded case: an array with a shape and no declared grid |
| `CRS_MISMATCH` / `AXIS_ORDER_MISMATCH` | not comparable as laid out |
| `AXIS_DIRECTION_MISMATCH` | the same footprint is traversed in opposite array directions |
| `ROTATED_OR_SKEWED` / `DEGENERATE_GRID` | not a cell-index operation |
| `OUTSIDE_TARGET_EXTENT` | aligned lattice, but covers ground the target does not |

Only `EXACT_MATCH` and `INTEGER_REFINEMENT` are `placeable` without inventing
values. `INTEGER_COARSENING` requires an explicit upsampling transformation;
`REQUIRES_DECLARED_RESAMPLING` and `AXIS_DIRECTION_MISMATCH` likewise require
declared transformations. Saying "yes" to any of those is how a silent regrid,
replication, or vertical mirror gets back in.

## What the recorded case actually was

The old error text named two shapes and nothing else, which is itself the
evidence: the only information available at the failure site was two shapes,
because the producer declared no georeference for its output. That is
`UNDEFINED_NO_GEOREFERENCE`, and the new message names the missing thing rather
than the symptom.

The real configuration explains why this was never a reshape:

- `outputs.pixel_m: 900` — the analysis cube is 900 m.
- `domain.resolutions_m: [9000, 3000, 1000]` — the innermost nest is 1000 m.
- `domain.fire_mesh_ratio: 10` — the SFIRE fire mesh is 100 m.

1000 m against 900 m is not an integer ratio in either direction, and the
253 km nest covers a fraction of the 900.9 km cube. Any code that had
"successfully" reshaped a (253,253) array into a (1001,1001) grid would have
produced a scientifically wrong answer *silently*, which is worse than the
dead-letter. The 100 m fire spacing has an integer 9:1 ratio to 900 m, but that
does not prove placement: CRS, origin, extent, axis direction, and whole-block
coverage still have to agree. The fix is for the producer to declare its exact
grid, not to relax the check.

One subtlety is enforced rather than assumed: an aligned 9× refinement is only
exact if the source spans **whole** 9-cell blocks. 253 is 28 blocks plus one
cell, so the edge target cell would be partly covered, and deciding what to do
about that is a resampling policy. That case is refused too.

## The producer half, and what real WRF output actually says

`models/wrf_georeference.py` derives a **verified** `GridDescriptor` from a
wrfout file's own metadata, so the contract has a source grid to reason about.
It was written against real WRF-SFIRE output on disk
(`wrf-sfire-stack/WRF-SFIRE/test/em_real/wrfout_d0?_2019-09-04_12:00:00`),
not against assumptions. Three things it found:

**The origin cannot be computed from `CEN_LAT`/`CEN_LON`.** Doing so puts the
domain 1.7° of latitude out. The origin is taken from the file's own
`XLONG`/`XLAT` corner and then verified against the entire coordinate field;
a derivation that misses WRF's coordinates by more than 30 m is refused rather
than returned. Measured agreement on the reference file is 2.7–3.4 m across
all three nests, a fraction of one 90 m fire cell.

**The fire subgrid is padded, and its arrays disagree about where they end.**
`west_east` is 213, `west_east_stag` is 214, `west_east_subgrid` is
2140 = 214 × 10. On that one allocated array:

| variable | populated to | why |
|---|---|---|
| `TIGN_G`, `LFN` | 2130 | true fire extent, 213 × 10 |
| `FXLONG` | 2131 | one halo coordinate column |
| `NFUEL_CAT` | 2140 | ingested input, fills the padding too |

Three arrays on one subgrid, three answers. An array's own extent therefore
cannot establish its grid, and block-reducing the full 2140 folds ten columns
of padded fuel into the result silently.

**The blocker is the projection, not the shape.** WRF writes a domain-centred
Lambert Conformal on a 6,370 km sphere, with no EPSG code; the analysis cube is
UTM. Placement of real d03 output onto a UTM cube returns `CRS_MISMATCH` — no
reshape reconciles that, and the old shape check could never have said so.

Within WRF's own CRS, an aligned target constructed on the same lattice can
recognize the 90 m fire mesh versus 900 m target as a whole-blocked 10×
`INTEGER_REFINEMENT`. The actual analysis cube is UTM, so that same native
output is not publishable there without a declared reprojection and the
variable-specific rules below.

## What a declared regrid would have to guarantee

Placement says a transformation is *required*. `transformations/resampling.py`
says what that transformation has to preserve, per variable, because the five
`outputs.targets` do not share an aggregation. The declarations are read off
WRF-SFIRE's own `Registry/registry.fire`, not inferred from names:

| cube variable | source | registry description | aggregation |
|---|---|---|---|
| `arrival_s` | `TIGN_G` | "ignition time on ground" (s) | earliest — `MIN` |
| `fire_area` | `FIRE_AREA` | "fraction of cell area on fire" (1) | areal-fraction mean |
| `fuel_consumed` | `FUEL_FRAC` | "fuel remaining" (1) | areal-fraction mean |
| `fire_intensity` | `FGRNHFX` | ground heat flux (W/m²) | area-weighted mean |
| `ros_max` | `ROS` | rate of spread (m/s) | `MAX` |
| `nfuel_cat` | `NFUEL_CAT` | fuel data (categorical) | majority |

This does not overturn `models/wrf_sfire_adapter.py`, which already uses `min`
for `arrival_s` and `mean` for the fractions, and whose `[:H, :W]` truncation
does correctly drop the padding block. What the registry adds is the
*precondition* those choices depend on, which was never stated:

**A plain mean of per-cell fractions equals the area-weighted mean only when
the cells in a block are equal-area.** That holds for an aligned integer
refinement inside one CRS. It stops holding across a reprojection, because
WRF's Lambert Conformal preserves angles, not areas — so cell areas vary across
the domain. An order statistic (`MIN`, `MAX`) and a mode are indifferent to
cell area; a mean is not.

So under `REQUIRES_DECLARED_RESAMPLING` every target is refused — no
transformation is declared — but they are refused for two *different* reasons,
and each refusal names what would settle it: an area-weighted regrid for the
three means, an order-preserving regrid for `arrival_s` and `ros_max`, and
nearest/majority for `nfuel_cat`. A refusal that only says "no" moves the
guessing rather than removing it.

Integer *coarsening* is refused outright for every variable: it is replication,
not aggregation, and would claim that every 90 m subcell ignited at the same
instant.

## The transformation that now exists

`ValueSemantics`' own docstring named the gap: *"Categorical and extensive
fields are outside the bilinear MVP instead of being silently interpolated."*
The bilinear regrid and reproject kinds admit only
`SCALAR_CONTINUOUS_INTENSIVE`. So of the six declared variables, exactly one
(`fire_intensity`) had any admissible transformation at all — an arrival time,
an extremum, an areal fraction and a category label are none of them
interpolatable.

They are, however, exactly **aggregatable** over a block partition.
`TransformationKind.SPATIAL_BLOCK_AGGREGATE` admits that case, with the
operation `transform.spatial_block_aggregate.v1` and binder
`transform.spatial_block_aggregate.bind.v1`. Four `ValueSemantics` members were
added to type the fields the docstring excluded: `FIRST_OCCURRENCE_TIME`,
`SCALAR_EXTREMUM`, `AREAL_FRACTION`, `CATEGORICAL_LABEL`.

The value class determines the aggregation — a bijection, not a parameter. The
`aggregation` parameter may only restate what the declared semantics already
imply, which is what stops a mean being applied to an arrival time by writing a
different string in the parameters. Undeclared classes (`UNSPECIFIED`, the
vector classes) admit nothing.

It refuses: a CRS change (aggregation is an index operation, not a
reprojection), a partial trailing block, cell sizes that disagree with the
block factors, and an offset lattice. Category majorities resolve ties to the
smallest label so a result never depends on iteration order.

**This does not solve WRF→cube.** It is same-CRS only, so it makes the 90 m
fire mesh → 900 m cube reduction legal *once the projection question is
settled* — either by a declared reprojection, which still does not exist, or by
defining the cube on WRF's own grid, which is a design decision rather than a
missing component.

## Test coverage

- `tests/test_placement_contract.py` — every grid is built from the
  real project configuration rather than convenient numbers.
- `tests/test_cube_preflight.py` — reconstructs the recorded
  `outputs.targets` plan and refuses it before the run.
- `tests/test_wrf_georeference.py` — the real-file lane asserts only
  what was read off the files and skips when they are absent (they are
  gitignored, 131 MB each); the synthetic lane covers every refusal path and
  runs everywhere.
- `tests/test_resampling_rules.py` — covers the declared aggregations
  and their preconditions.
- `tests/test_stage4_block_aggregate.py` — covers the new
  transformation: its declaration, its four refusals, and the execution
  semantics of each aggregation.

Adding the operation and binder cost **one** test change — the pinned binder
tuple in `tests/test_stage2_capabilities.py`, which is exactly what that test
exists to catch. An earlier note in this file warned it would invalidate every
digest; that was wrong. Digests are computed from source at call time and no
fixture persists one, so nothing went stale.

It did expose a real asymmetry: the binder registry is pinned to an exact
tuple, but the operation registry was only `issubset`-checked, so the new
operation landed without any test noticing. `tests/test_stage4_runtime_ops.py`
now pins the `transform.*` operations exactly as well.

## What this does not do

- **It does not resample.** There is no regrid here, by design. The verdict
  points at a declared transformation; it does not perform one.
- **It is not wired into an automatic launch path.** The preflight is callable
  and tested, while the retained legacy cube write still has its final shape
  check. The current target resolver does not yet compile and launch arbitrary
  producer plans, and the legacy cascade does not call this whole-plan
  preflight before WRF compute.
- **The WRF reader, georeference, and runtime publication path are not joined.**
  `models/wrf_georeference.py` produces the grid declaration, but the retained
  adapter does not return it through the current authoritative artifact runtime.
  Connecting those pieces still would not settle the WRF→cube reprojection that
  `CRS_MISMATCH` demands.
- **No reprojection is declared.** `SPATIAL_BLOCK_AGGREGATE` is same-CRS by
  construction and refuses to change CRS. The WRF-Lambert→UTM transformation
  still does not exist, so WRF output still cannot be published to a UTM cube.
- **No area-weighted regrid exists.** The plain mean is exact only because the
  block cover is same-CRS and equal-area. Across a reprojection it is not, and
  nothing here implements the area-weighted version that case needs.
- **Nothing is wired end to end.** The transformation can be declared and
  executed on a `field-json-v2` value, but no capability in the catalog emits
  one, and the WRF adapter does not produce `field-json-v2`.
- **Only the 12:00 files were read.** The georeference is time-invariant, so
  this is sound for grid geometry, but no time series was inspected.
- **It does not unblock Stage 9B.** R1 still has no promoted golden fixture and
  no provider is certified. This removes one blocker of several.
- **Vertical and temporal placement are out of scope.** Only the horizontal
  grid relationship is decided.
