# NASA Project documentation

This repository contains the maintained design and roadmap for the
target-driven scientific workflow composer in the sibling `NASA_Project`
repository.

## Current sources of truth

- `design_explained.md` — the implemented artifact-first architecture.
- `scientific_workflow_composition_plan.md` — current scope, verified state,
  next stage, exit gates, and explicit non-claims.

## Research roadmap

- [agentic_plan.md](agentic_plan.md) — research-backed plan for heterogeneous LLM, symbolic,
  retrieval, supervised-ML, GNN, bandit, Bayesian-optimization, RL/MARL,
  critic, and human agents around the verified workflow kernel.
- [nasa_technical_report.md](nasa_technical_report.md) — concise NASA-facing
  report on asteroid cascade-hazard workflow composition, scalable
  orchestration, WRF-SFIRE integration, domain-expert use, and prospective
  agentic research.

The agentic plan is a proposed research program, not a claim that those agents
are currently implemented or authorized to execute scientific work.

Older Cube-authority diagrams, slide decks, generated office files, and the
pre-remediation roadmap were removed because they contradicted the current
implementation. Their history remains available in Git.

## Current focus

The user requests a typed scientific target. The system searches complete
artifact metadata, reuses compatible native files when available, otherwise
selects a declared producer workflow, and automatically registers a native
output after its fenced runtime commit. Native payloads are not copied,
reprojected, transformed, or forced into Cube/Zarr by this path.

The implementation is a bounded same-node prototype. It does not claim a real
WRF-SFIRE run, real Slurm deployment, remote-provider completeness, or
production-scale catalog/event throughput.
