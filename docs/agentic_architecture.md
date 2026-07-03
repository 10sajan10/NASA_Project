# Agentic Cascade Architecture

Three views: the layered system, the full agent workflow, and the
asteroid-impact example flowing through it.

## 1. Overall architecture (layers)

```mermaid
flowchart TB
    subgraph ENTRY["ENTRY POINTS"]
        Q["Natural-language question<br/>scripts/ask_cascade.py"]
        EV["Event JSON drop<br/>events/inbox + scripts/watch_events.py"]
        MCPC["Claude Code / any MCP client<br/>agentic/mcp_server.py"]
        CLI["Human CLI<br/>scripts/run_cascade.py"]
    end

    subgraph AGENTIC["AGENTIC LAYER (agentic/) — decides WHAT and WHICH"]
        AGENT["PlannerAgent — agent.py<br/>Claude tool-use loop (claude-opus-4-8, adaptive thinking)<br/>parses question to EventSpec + intent, revises on PlanError"]
        TOOLS["Tool surface — tools.py (JSON in/out, never raises)<br/>describe_ontology | search_datasets | search_models<br/>resolve_plan | estimate_cost | submit_plan"]
        EVH["Event bus — events.py<br/>normalize_event → handle_event → report.json"]
        PLAN["Deterministic planner — planner.py<br/>1 intent → target variables<br/>2 backward-chain over cards<br/>3 filter: coverage bbox+time, regime, trust, exclusions<br/>4 score: fidelity &gt; trust &gt; regime-match &gt; cost<br/>5 resolution ladder vs ComputeBudget (cost models)<br/>→ RunPlan: bindings + alternatives + reasons"]
        META["MetaCatalog (DuckDB) — metacatalog.py<br/>DatasetCards + ModelCards: coverage, provenance,<br/>trust tier, valid regimes, cost model<br/>↔ configs/registry.json (human-readable)"]
        ONT["Ontology — ontology.py<br/>controlled variable vocabulary<br/>(gate at card registration)"]
        EXEC["Executor — executor.py<br/>RunPlan → run_cascade.py argv<br/>(--dry-run by default)"]
        CRITIC["Critic — critic.py<br/>presence | footprint coverage | sanity bounds<br/>→ exclude failed producer → replan"]
    end

    subgraph ENGINE["ENGINE SUBSTRATE (engine/) — decides HOW, executes"]
        CAT["CascadeCatalog — models/catalog.py<br/>producer factories + cards<br/>build_registry(ctx, only=plan bindings)"]
        PIPE["Pipeline.from_targets<br/>backward-chained DAG"]
        RUN["PipelineRunner<br/>backends: serial/thread/process/dask/SLURM<br/>retries + dead-letter, tile fan-out,<br/>dirty propagation, lineage, content cache"]
    end

    subgraph PROD["PRODUCERS (ProducerV2 contract)"]
        DRV["Drivers: thermal KML | landfire | dem<br/>era5_wind | exposure (synthetic)"]
        MDL["Models: impact_scaling | blast_damage<br/>econ_loss | wrf_sfire (+chem)"]
    end

    subgraph DATA["CUBE (cube/)"]
        CUBE["Zarr store per variable + DuckDB catalog v2<br/>provenance: source_url, license, checksum, run_id<br/>resolution-aware satisfies(), snapshots"]
    end

    Q --> AGENT
    EV --> EVH
    MCPC --> TOOLS
    AGENT <--> TOOLS
    TOOLS --> PLAN
    EVH --> PLAN
    PLAN <--> META
    ONT -.validates.- META
    PLAN -- validated RunPlan --> EXEC
    EXEC --> CLI
    CLI --> CAT
    CAT --> PIPE --> RUN
    RUN --> DRV & MDL
    DRV & MDL -- write variables --> CUBE
    CUBE -- satisfies()/skip + reads --> RUN
    CUBE -- outputs --> CRITIC
    CRITIC -- "replan(exclude=failed producers)" --> PLAN
```

Design contract: **the LLM proposes, the engine disposes.** The agent
only selects among registered producers and binds parameters; the
accepted plan always comes from `planner.plan()` (re-validated by
`submit_plan`), and execution goes through the same
`run_cascade.py` → engine path humans use — one code path, one lineage
record.

## 2. Agent workflow (end to end)

```mermaid
sequenceDiagram
    autonumber
    actor U as User / Event feed
    participant A as PlannerAgent (Claude)
    participant T as Tool surface (deterministic)
    participant P as planner.plan()
    participant M as MetaCatalog (cards)
    participant X as Executor → run_cascade.py
    participant E as Engine (registry → DAG → runner)
    participant C as Cube (Zarr + DuckDB)
    participant K as Critic

    U->>A: "economic consequences of a 5 Mt airburst over Dallas?"
    A->>T: describe_ontology()
    T-->>A: variable vocabulary by domain
    A->>T: search_models / search_datasets (coverage, regime, trust)
    T->>M: SQL filters
    M-->>A: surviving candidates + metadata
    A->>T: resolve_plan(event, targets, budget)   [dry run]
    T->>P: plan()
    P->>M: candidates_for(each variable, filtered)
    alt unsatisfiable
        P-->>A: PlanError: variable + dependency chain
        A->>T: revised resolve_plan(...)           [feedback loop]
    end
    A->>T: estimate_cost(event) — resolution vs cores table
    A->>T: submit_plan(event, targets, budget, rationale)
    T->>P: plan()  [re-validation — accepted plan is planner output]
    P-->>A: RunPlan: bindings, alternatives, reasons, resolution, est cost
    A-->>U: plain-language summary + rationale
    U->>X: --execute (--for-real to consume compute)
    X->>E: run_cascade --targets ... --pixel-m ... --np ...
    E->>E: build_registry(only=plan producers)<br/>Pipeline.from_targets (backward chain)
    E->>C: producers write variables (skip if cube.satisfies)
    C-->>E: lineage: run_id, checksums, versions
    E-->>K: run complete
    K->>C: check each target: present? coverage? sane bounds?
    alt findings
        K->>P: replan(exclude=failed producers)
        P-->>X: new RunPlan (next-best candidates)
        X->>E: re-execute (dirty propagation recomputes only stale parts)
    else clean
        K-->>U: verified report (plan + coverage + lineage)
    end
```

## 3. Example: cascading impact of an asteroid event

```mermaid
flowchart LR
    EVJ["EVENT<br/>kind: asteroid_impact<br/>lat 32.78, lon −96.81 (Dallas)<br/>energy_mt: 5.0, radius 50 km"]
    NORM["normalize_event → EventSpec<br/>intent → targets<br/>economic → economic_loss_usd<br/>fire → arrival_s, fire_area<br/>air_quality → pm25_surface"]

    EVJ --> NORM

    subgraph CONS["CONSEQUENCE BRANCH — intent=economic (30 m, ~seconds)"]
        IMP["impact_scaling<br/>Collins scaling laws<br/>(scaling-law, validated)"]
        EXPO["exposure driver<br/>population + assets<br/>(experimental, synthetic)"]
        DMG["blast_damage<br/>logistic vulnerability<br/>p50 = 35 kPa"]
        ECON["econ_loss<br/>HAZUS-style<br/>damage × assets × 1.45"]
        IMP -- blast_overpressure_pa --> DMG
        DMG -- building_damage_frac --> ECON
        EXPO -- asset_value_usd --> ECON
        EXPO -- population_density --> ECON
    end

    subgraph FIRE["ATMOSPHERIC BRANCH — intent=fire / air_quality (100 m+, ~hours)"]
        THM["thermal driver<br/>PDC KML footprint"]
        LF["landfire<br/>FBFM13 fuels (30 m, CONUS)"]
        DEM["dem driver<br/>SRTM/3DEP (30 m)"]
        ERA["era5_wind<br/>ECMWF reanalysis"]
        WRF["wrf_sfire (+chem)<br/>coupled fire–atmosphere<br/>(full-physics, MPI)"]
        THM -- ignition_t0 --> WRF
        LF -- nfuel_cat --> WRF
        DEM -- dem --> WRF
        ERA -- "wind_speed_ms, wind_dir_deg" --> WRF
    end

    NORM -- "targets = [economic_loss_usd]" --> ECON
    NORM -. "targets = [arrival_s, pm25_surface]" .-> WRF

    ECON -- "economic_loss_usd<br/>population_exposure" --> CUBE["CUBE<br/>+ provenance + lineage"]
    WRF -- "arrival_s, fire_area, ros_max<br/>pm25_surface, smoke_tracer" --> CUBE

    CUBE --> CRIT["CRITIC<br/>coverage ≥ 60%? bounds sane?<br/>damage ∈ [0,1], loss ≥ 0"]
    CRIT -- "ok → verified report" --> OUT["REPORT<br/>plan + bindings + alternatives<br/>+ rationale + lineage"]
    CRIT -- "failure → exclude producer,<br/>replan next-best" --> NORM
```

Key behaviour shown here: **focus falls out of backward-chaining.**
Asking for `economic_loss_usd` builds only the consequence branch —
the WRF chain is never constructed, fetched, or paid for. Asking
`full_cascade` builds both. If two producers can supply the same
variable (e.g. a real WorldPop driver alongside the synthetic exposure
driver), the planner's deterministic scoring picks one and records the
other as an alternative — which is exactly what the critic's replan
falls back to when an output fails verification.
