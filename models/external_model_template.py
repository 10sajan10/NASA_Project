"""Drop-in template for plugging an external model into the engine.

Copy this file to ``models/<your_model>.py``, rename the class, change
the imports/binary path, and customize the three model-specific hooks.
The engine handles everything else: dependency resolution, skip when
satisfied, dirty propagation, retries, parallel execution, lineage.

This template assumes a model whose:
  * inputs are loaded from disk (NetCDF / NumPy / namelist / CSV / etc.)
  * runs as a subprocess (or any callable that takes a directory of inputs)
  * outputs are written to disk and parsed back

For pure-Python models that don't need a subprocess, subclass
``engine.ProducerV2`` directly with `extract` / `compute` / `update`
(see `models/lfmc_v2.py` for an example).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from engine.contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    Request,
    VarSpec,
)
from engine.data_adapter import DataAdapter, DataNeed
from engine.model_adapter import ModelAdapter


class ExternalModelTemplate(ModelAdapter):
    """Generic external-model plug-in.

    Replace the data_adapter / produces / hook bodies with what your
    model actually needs. The class attributes below are the public
    contract the engine reads.
    """

    # ---- 1. Identity ---------------------------------------------------
    name = "external_model_template"

    # ---- 2. Declared inputs (with optional resolution constraints) -----
    # Each DataNeed becomes a VarSpec the engine searches for in the cube.
    # If a need declares max_native_res_m and the cached data is coarser,
    # the engine treats the input as unsatisfied and walks the DAG back
    # to re-fetch / re-compute at the required resolution.
    data_adapter = DataAdapter([
        DataNeed("input_a",  kind="static", max_native_res_m=30.0,
                  description="Example fine-resolution static input"),
        DataNeed("input_b",  kind="static", max_native_res_m=100.0,
                  description="Coarser static input"),
        DataNeed("input_ts", kind="time",   description="Example time series"),
        DataNeed("optional_aux", kind="static", required=False,
                  description="Optional input; produces None if absent"),
    ])

    # ---- 3. Declared outputs (each with its own merge policy) ----------
    # MergePolicy controls what happens when two writes target the same
    # variable. LAST_WRITER is the default; MONOTONE_MIN/MAX preserve
    # cell-wise extremes across multiple invocations.
    produces = (
        VarSpec("model_output_main", kind="static", units="",
                merge_policy=MergePolicy.LAST_WRITER),
        VarSpec("model_output_extra", kind="static", units="",
                merge_policy=MergePolicy.MONOTONE_MAX),
    )

    # ---- 4. Scheduler hints --------------------------------------------
    capabilities = ProducerCapabilities(
        tile_parallel=False,         # external models usually run whole-grid
        cost_hint=CostHint.CPU,      # or IO for network-bound, GPU for accel
        memory_budget_mb=2048,
        iterative=False)

    # ---- 5. Constructor ------------------------------------------------
    def __init__(self, binary: str | Path,
                 *, stage_root: str | Path | None = None,
                 keep_stage: bool = False) -> None:
        self.binary = str(binary)
        if stage_root is not None:
            self.stage_root = Path(stage_root)
        self.keep_stage = keep_stage

    # ---- 6. The three model-specific hooks -----------------------------
    def stage_inputs(self, grid, inputs: dict[str, Any],
                     request: Request, stage: Path) -> None:
        """Write `inputs` into whatever format your model expects.

        Args:
          grid    : SimulationGrid (height, width, pixel_m, crs_epsg, x0, y1)
          inputs  : dict from DataAdapter.fetch():
                      - static needs map to ndarray
                      - time needs map to (timestamps_list, ndarray)
                      - optional+absent needs map to None
          request : Request with t_start, t_end, context dict
          stage   : Path to a fresh empty directory unique to this run
        """
        # Example: persist arrays as .npy + a small metadata JSON.
        # Replace with your model's actual input format (NetCDF, namelist,
        # CSV, GeoTIFF, whatever).
        for name in ("input_a", "input_b"):
            np.save(stage / f"{name}.npy", inputs[name])
        ts, ts_arr = inputs["input_ts"]
        np.save(stage / "input_ts.npy", ts_arr)
        (stage / "config.json").write_text(json.dumps({
            "grid_height": grid.height,
            "grid_width":  grid.width,
            "pixel_m":     grid.pixel_m,
            "n_timesteps": len(ts),
            "has_optional": inputs["optional_aux"] is not None,
            "t_start": str(request.t_start),
            "t_end":   str(request.t_end),
        }))

    def run_model(self, stage: Path, request: Request) -> Path:
        """Invoke the model. Return the path containing outputs.

        Anything raised here propagates. If your model is flaky, the
        engine's RetryPolicy can retry transient failures (configure on
        the PipelineRunner).
        """
        subprocess.run(
            [sys.executable, self.binary, str(stage)],
            cwd=stage, check=True)
        return stage  # outputs live in the stage dir

    def parse_outputs(self, output_path: Path,
                      grid) -> dict[str, Any]:
        """Read the model's outputs and return arrays keyed by the names
        declared in `produces`. Shapes must match the cube grid.
        """
        return {
            "model_output_main":  np.load(output_path / "main.npy"),
            "model_output_extra": np.load(output_path / "extra.npy"),
        }


# ---------------------------------------------------------------- usage
# How a scenario wires this in:
#
#     from engine import (Pipeline, PipelineRunner, RetryPolicy,
#                         to_engine_registry, make_backend)
#     from models.external_model_template import ExternalModelTemplate
#
#     model = ExternalModelTemplate(binary="/path/to/your/model.py")
#     reg = to_engine_registry([model, ...upstream producers...])
#     pipeline = Pipeline.from_targets(
#         ["model_output_main", "model_output_extra"], registry=reg)
#
#     backend = make_backend("thread", max_workers=4)        # or process / dask
#     runner = PipelineRunner(reg, backend=backend,
#                              retry_policy=RetryPolicy(max_attempts=3))
#     result = runner.run(cube, pipeline,
#                          t_start=..., t_end=...)
#
# That's it. Add a new model? Subclass ModelAdapter; the engine never
# changes.
