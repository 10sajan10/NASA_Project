# Stage 9B — not started; its blocker is now typed

Status: **Stage 9B has not begun, and could not honestly begin.** Its own
entry gates fail. What this stage folder contains is the one prerequisite that
was buildable without running WRF and without inventing evidence: an explicit
placement contract for the recurrent 253/1001 defect.

## Why 9B did not start

The roadmap opens Stage 9B with: *"Begin after Stage 6, WRF reference lane R1
(and R2 for real-data mode), and one provider certified for the selected
ExecutionProfile."* All three conditions are unmet:

| Gate | State |
|---|---|
| R1 golden fixture | none promoted — `stage0/wrf_interface_audit.md`: *"no run is promoted as a trusted golden scientific fixture in Stage 0"* |
| R2 / R2A real-data lane | not built |
| one certified provider | Stage 9A is simulated only, never submitted a job, not wired into the controller, and has no MPI gang task |

The same audit is blunt about the consequence: *"Atmospheric WRF cannot compete
as a wind producer until its standalone reference gate passes"* and *"The
253/1001 mapping defect is a blocker for WRF output publication."*

Starting 9B on top of that would mean using WRF as the first runtime
correctness test — the one thing the roadmap explicitly forbids.

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
`cube/preflight.py` runs it across a whole publication plan up front.

### The check asked the wrong question

"Shape mismatch" is the symptom. The real question is whether a *declared
relationship* exists between the two grids. Shape alone answers it in neither
direction:

- A 100 m fire mesh and a 900 m cube have different shapes and place
  **exactly** — 900/100 = 9, a clean block aggregation.
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
| `ROTATED_OR_SKEWED` / `DEGENERATE_GRID` | not a cell-index operation |
| `OUTSIDE_TARGET_EXTENT` | aligned lattice, but covers ground the target does not |

Only the first three are `placeable`. `REQUIRES_DECLARED_RESAMPLING`
deliberately is **not** — the data may well be usable, but only through an
explicit Stage-4 transformation carrying its own cost and assumptions. Saying
"yes" there is exactly how a silent regrid gets back in.

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
dead-letter. The 100 m fire mesh, by contrast, aggregates onto the cube
exactly — so the fix is for the producer to declare which grid its output is
on, not to relax the check.

One subtlety is enforced rather than assumed: an aligned 9× refinement is only
exact if the source spans **whole** 9-cell blocks. 253 is 28 blocks plus one
cell, so the edge target cell would be partly covered, and deciding what to do
about that is a resampling policy. That case is refused too.

## Test coverage

- `tests/test_placement_contract.py` — 18 tests. Every grid is built from the
  real project configuration rather than convenient numbers.
- `tests/test_cube_preflight.py` — 10 tests reconstructing the recorded
  `outputs.targets` plan and refusing it before the run.

Suite: 772 passed, 1 skipped, 7 xfailed.

## What this does not do

- **It does not resample.** There is no regrid here, by design. The verdict
  points at a declared transformation; it does not perform one.
- **It is not wired into `cube/store.py`.** The write-time check still stands
  as the last line of defence. Making the legacy v1 cascade call the preflight
  is a change to the running pipeline and was not made without a way to
  exercise it end to end.
- **No WRF output was inspected.** The producer-side grid must come from the
  adapter declaring its output georeference, and `models/wrf_sfire_adapter.py`
  is user-owned and untouched. That declaration is the remaining half of the
  fix and is not written.
- **It does not unblock Stage 9B.** R1 still has no promoted golden fixture and
  no provider is certified. This removes one blocker of several.
- **Vertical and temporal placement are out of scope.** Only the horizontal
  grid relationship is decided.
