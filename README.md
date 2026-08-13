# Wildfire Simulation Engine

A model-agnostic, dependency-resolved, variable-centric orchestration
engine for spatial / spatiotemporal scientific simulations. Plug in any
external model; the engine handles dependency resolution, caching,
parallelism, retries, lineage, and reproducibility.

This repository is the substrate, not a fixed pipeline. There is no
baked-in fire-spread algorithm, no required data source, no scenario
glue. You bring the model and the data; the engine wires them together.

> **Current build status:** [Stage 0](stage0/) froze the baseline and kernel
> invariants. [Stage 0A](stage0a/) selected the runtime substrate. [Stage 1](stage1/)
> now provides the durable single-node controller, supervised subprocesses,
> fenced validation/commit, and restart tests for an already-bound graph.
> WRF-SFIRE was not run; MPI/SLURM remain conditional future provider
> capabilities and are not available on this private development node.

## Components

| Layer | Purpose |
|---|---|
| [cube/](cube/) | Per-variable Zarr storage indexed by a DuckDB catalog. Resolution-aware satisfaction checks, schema versioning, halo I/O, snapshots. |
| [engine/](engine/) | Orchestration substrate. ProducerV2 contract, DAG pipeline DSL, pluggable execution backends (serial / thread / process / dask / SLURM), tile fan-out, retries with dead-letter, dirty propagation, run lineage, content-addressable cache, disk-spill workspace. |
| [drivers/](drivers/) | Data-source-specific fetchers (KML, thermal-pulse, DEM, weather reanalysis, etc). These are scenario-specific — keep what you need, write more as you go. |
| [models/](models/) | `external_model_template.py` — drop-in template for new model adapters. `wrf_sfire_adapter.py` — adapter that calls the external WRF-SFIRE model in [wrf-sfire/](wrf-sfire/). No physics ships here; adapters are wiring. |
| [agentic/](agentic/) | Agent-queryable metadata + deterministic planning. Variable ontology, DuckDB metacatalog of dataset/model cards (coverage, provenance, regimes, cost models), `EventSpec -> RunPlan` planner, and a JSON tool surface for an LLM planner agent. |
| [tests/](tests/) | Engine guarantees end-to-end. |

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

## Agentic planning (event -> plan -> run)

The LLM proposes, the engine disposes. An agent only *selects* among
registered producers and binds parameters; it never generates glue code.

```python
from models.catalog import default_catalog, make_context
from agentic import MetaCatalog, EventSpec, ComputeBudget, plan
from engine import Pipeline

cat = default_catalog()
mc = MetaCatalog("configs/registry.duckdb")
cat.seed_metacatalog(mc)                  # publish cards for search

ev = EventSpec(kind="asteroid_impact", lat=32.78, lon=-96.81,
               magnitude={"energy_mt": 5.0}, intent="economic")
p = plan(ev, mc, budget=ComputeBudget(cores=56, wall_s=12*3600))
# p.bindings   : variable -> chosen producer + score + alternatives + reason
# p.resolution_m, p.est_wall_s : finest resolution that fits the budget

reg = cat.build_registry(ctx, only=set(p.producer_names()))
pipe = Pipeline.from_targets(p.targets, registry=reg)
```

Because the resolver backward-chains from targets, `intent="economic"`
pulls only impact -> damage -> exposure -> economy; the atmospheric
chain is never built. `agentic/tools.py` wraps search/plan/estimate as
JSON-in/JSON-out functions for an LLM agent (MCP-ready); PlanError
messages name the unsatisfiable variable and chain, so the agent can
revise instead of hallucinate.

### Natural-language planning (Phase 2)

`agentic/agent.py` runs a Claude tool-use loop over that surface —
the model only *selects and binds*; the accepted plan is always
re-validated by the deterministic planner (`submit_plan`), never
model-generated JSON. Rejections flow back as structured tool results,
so the agent revises against real resolver feedback.

```bash
# natural language -> validated plan (ANTHROPIC_API_KEY or `ant auth login`)
python scripts/ask_cascade.py \
    "What are the economic consequences of a 5 Mt airburst over Dallas?"

# ... then preview the resolved DAG, or actually run it
python scripts/ask_cascade.py "..." --execute            # run_cascade --dry-run
python scripts/ask_cascade.py "..." --execute --for-real

# expose the catalog/planner to Claude Code / any MCP client
python -m agentic.mcp_server
```

Execution goes through `scripts/run_cascade.py` (one code path, one
lineage record); `agentic/executor.py` maps a validated plan onto its
flags, defaulting to `--dry-run`. The loop is fully tested offline with
a scripted fake client (`tests/test_agent_loop.py`).

### Event-driven autonomy + critic loop (Phase 3)

The asteroid consequence chain is now real: `impact_scaling`
(Collins-style blast footprint, scaling-law tier) -> `blast_damage`
(logistic vulnerability curve) -> `econ_loss` (HAZUS-style loss +
population exposure), fed by a synthetic exposure driver (replace with
WorldPop/HAZUS drivers when licensed data lands — the planner will
prefer them automatically via trust tiers). Pure Python, no network;
`tests/test_cascade_consequence.py` runs it end-to-end through the
engine.

```bash
# drop an event, get a plan report (add --execute / --for-real to run)
echo '{"kind": "asteroid_impact", "lat": 32.78, "lon": -96.81,
       "energy_mt": 5.0, "intent": "economic"}' > events/inbox/dallas.json
python scripts/watch_events.py --once
cat events/processed/dallas.report.json
```

After a run, `agentic/critic.py` verifies each planned target against
the cube — presence, coverage of the event footprint, physical sanity
bounds — and a failed check excludes the responsible producer and
replans onto the next-best candidate (`plan(exclude=...)`):

    plan -> execute -> critique -> replan (with exclusions) -> ...

Dirty propagation already handles the reactive case: a re-fetched
upstream observation invalidates downstream outputs and the next run
recomputes only the stale part of the chain.

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
  external_model_template.py    drop-in adapter template
  wrf_sfire_adapter.py          adapter for the external WRF-SFIRE model
tests/          engine + adapter integration tests
configs/        example configs
```
