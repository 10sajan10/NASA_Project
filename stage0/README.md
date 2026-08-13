# Stage 0 — Baseline and invariant freeze

Status: **complete for the composition/runtime critical path** on 2026-08-12.
The WRF reference-science work remains a separate lane (`R0` onward); Stage 0
does not claim that WRF-SFIRE is scientifically validated.

This directory is the evidence for the first roadmap gate. Stage 0 deliberately
does not implement the future resolver, durable runtime, or SLURM provider. It
freezes what exists so those stages have a reproducible starting point.

## Completed gate evidence

- `reference/reduced_consequence_manifest.json` is a retained, network-free,
  four-component reference execution with exact plan/component hashes and
  deterministic output hashes.
- `baseline_report.md` records the full test baseline and strict quarantine.
- `runtime_invariants.md` freezes Stage-0 identity and execution invariants.
- `wrf_interface_audit.md` separates idealized WRF-SFIRE, real-data WRF-SFIRE,
  and atmospheric-WRF producer interfaces and classifies reproduced logs.
- `cluster_access_profile.json` distinguishes verified, unavailable, and
  unknown site facts. It does not infer authorization from installed commands.
- `slurm_provider_requirements.md` makes SLURM a required future execution
  capability even though it is unavailable to the current process.
- `wind_evidence_inventory_v0.json` records current wind options and, more
  importantly, the evidence that is still missing.

## Source changes in this gate

- Pipelines bind exact producer objects, implementation hashes, configuration
  hashes, declarations, and resource hints before execution.
- The runner never resolves a bound node through the mutable registry.
- Mutation after binding is rejected.
- Runtime trigger expansion is disabled by default; legacy tests opt in
  explicitly until child-plan revisions replace it.
- Every run carries a `stage0-execution-manifest-v1` record.
- Known runtime defects are executable strict-xfail tests, so they remain
  visible and an accidental XPASS fails the suite.

## Reproduce

```bash
.venv/bin/python -m pytest tests -q --disable-warnings --maxfail=0

work=$(mktemp -d /tmp/nasa-stage0.XXXXXX)
.venv/bin/python scripts/run_stage0_baseline.py \
  --workspace "$work" \
  --manifest stage0/reference/reduced_consequence_manifest.json
```

On this host, Zarr/async tests must be run outside the Codex filesystem sandbox;
the sandbox caused a wait in Zarr's asynchronous worker thread. This is recorded
as a harness limitation, not a source-code failure.
