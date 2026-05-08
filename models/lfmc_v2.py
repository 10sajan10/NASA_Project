"""LFMC model — ProducerV2 contract version.

Same Yebra et al. (2013) regression as the legacy `models/lfmc_model.py`
(LFMC_pct = 125 + 288 * NDWI), expressed as the new declarative
contract. Validates that the engine contract works against real model
code.

The legacy `lfmc_model.run(cube)` keeps working for existing callers;
this is an additive migration target — the engine's PipelineRunner can
now plug this producer in via:

    from engine import to_engine_registry, Pipeline, PipelineRunner
    from models.lfmc_v2 import LfmcModel

    reg = to_engine_registry([LfmcModel(), ...])
    pipeline = Pipeline.from_targets(["lfmc_pct"], registry=reg)
    PipelineRunner(reg).run(cube, pipeline)
"""
from __future__ import annotations

from typing import Any

import numpy as np

from engine.contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    ProducerV2,
    Request,
    VarSpec,
)


class LfmcModel(ProducerV2):
    """Live fuel moisture content from NDWI (Yebra 2013).

    A pure transform: read static `ndwi`, return static `lfmc_pct`.
    Cube I/O is handled by the inherited ProducerV2.update which honors
    the declared LAST_WRITER policy.
    """

    name = "lfmc_v2"
    requires = (VarSpec(name="ndwi", kind="static"),)
    produces = (VarSpec(
        name="lfmc_pct",
        kind="static",
        dtype="float32",
        units="%",
        merge_policy=MergePolicy.LAST_WRITER,
        description="Live fuel moisture content (Yebra 2013 regression: "
                    "LFMC_pct = 125 + 288 * NDWI; clipped to [30, 250])"),)
    capabilities = ProducerCapabilities(
        cost_hint=CostHint.CPU,
        memory_budget_mb=128)

    # Optional satisfaction check so re-runs against a populated cube
    # are skipped automatically by the runner.
    def is_satisfied(self, cube, variable: str, request: Request) -> bool:
        if request is not None and getattr(request, "force", False):
            return False
        return cube.has(variable)

    def extract(self, cube, request: Request) -> dict[str, Any]:
        return {"ndwi": cube.read_static("ndwi")}

    def compute(self, inputs: dict[str, Any],
                request: Request) -> dict[str, Any]:
        ndwi = inputs["ndwi"]
        lfmc = 125.0 + 288.0 * ndwi

        # Cells that were NaN through the whole NDWI window get filled with
        # the mosaic median; if everything was NaN, fall back to 100% which
        # is a benign mid-range default for moisture.
        median = float(np.nanmedian(lfmc))
        if not np.isfinite(median):
            median = 100.0
        lfmc = np.where(np.isfinite(lfmc), lfmc, median).astype(np.float32)
        lfmc = np.clip(lfmc, 30.0, 250.0)
        return {"lfmc_pct": lfmc}
