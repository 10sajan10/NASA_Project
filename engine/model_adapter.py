"""ModelAdapter: pluggable wrapper for any external model.

Plug in any model by subclassing `ModelAdapter` and implementing three
hooks. The engine handles every other concern: dependency resolution,
resolution-aware skip, dirty propagation, retries, lineage, parallelism.

The three hooks
---------------
    stage_inputs(grid, inputs, request, stage_dir) -> None
        Convert cube data (already fetched for you) into whatever
        files / namelists / NetCDFs / CSVs the model expects on disk.

    run_model(stage_dir, request) -> Path
        Invoke the model. Typically `subprocess.run([self.binary], ...)`
        with `cwd=stage_dir`. Returns the path that contains the model's
        outputs (often the same stage_dir).

    parse_outputs(output_path, grid) -> dict[str, ndarray]
        Read the model's output files and return arrays keyed by the
        VarSpec names declared in `produces`. The engine writes them
        back to the cube through ProducerV2.update (which honors each
        VarSpec's merge_policy).

Inputs are described declaratively via a `DataAdapter`. Outputs are
declared as a tuple of `VarSpec`s. Both are plain data — subclasses
just point at them.

Minimal example
---------------
    class MyModel(ModelAdapter):
        name = "my_model"
        data_adapter = DataAdapter([
            DataNeed("ndvi", kind="static", max_native_res_m=30.0),
            DataNeed("dem",  kind="static"),
        ])
        produces = (VarSpec("my_output", kind="static"),)
        capabilities = ProducerCapabilities(cost_hint=CostHint.CPU)

        def __init__(self, binary: str):
            super().__init__()
            self.binary = binary

        def stage_inputs(self, grid, inputs, request, stage):
            # write inputs/{ndvi,dem}.npy or .nc or whatever the binary wants
            ...

        def run_model(self, stage, request):
            subprocess.run([self.binary], cwd=stage, check=True)
            return stage / "output.nc"

        def parse_outputs(self, output_path, grid):
            with nc.Dataset(output_path) as ds:
                return {"my_output": np.asarray(ds.variables["x"][:])}

That's it. Register it like any other producer:
    reg.register(MyModel(binary="/path/to/bin"))
    PipelineRunner(reg).run(cube, Pipeline.from_targets(["my_output"], reg))
"""
from __future__ import annotations

import shutil
import tempfile
from abc import abstractmethod
from pathlib import Path
from typing import Any

from .contracts import ProducerCapabilities, ProducerV2, Request, VarSpec
from .data_adapter import DataAdapter


# Sentinel key used to pass grid metadata through extract -> compute
# without exposing it as a cube variable.
_GRID_KEY = "__grid__"


class ModelAdapter(ProducerV2):
    """Base class for plug-in external models.

    Subclass attributes (declarative):
      * ``name``         : unique producer name
      * ``data_adapter`` : DataAdapter describing inputs (with resolution)
      * ``produces``     : tuple[VarSpec, ...] describing outputs
      * ``capabilities`` : ProducerCapabilities (cost hint, memory, etc.)

    Subclass methods (the model-specific bits):
      * stage_inputs / run_model / parse_outputs
    """

    data_adapter: DataAdapter
    produces: tuple[VarSpec, ...] = ()
    capabilities: ProducerCapabilities = ProducerCapabilities()

    # Where to stage runs. Each call gets a fresh subdirectory under
    # `stage_root` so concurrent runs don't collide. Set to None to use
    # the OS tempdir (default), or override per-subclass.
    stage_root: Path | None = None

    # If False (default), the stage dir is removed after a successful
    # run. Set True if you want artifacts preserved for inspection /
    # debugging. Can also be set per-call via request.context["keep_stage"].
    keep_stage: bool = False

    # ------------------------------------------------------------------
    # ProducerV2 surface
    # ------------------------------------------------------------------
    @property
    def requires(self) -> tuple[VarSpec, ...]:  # type: ignore[override]
        return self.data_adapter.varspecs()

    def extract(self, cube, request: Request) -> dict[str, Any]:
        """Pull every declared input from the cube via DataAdapter, plus
        the cube grid (so the subclass can stage inputs in geometric
        coordinates)."""
        out: dict[str, Any] = self.data_adapter.fetch(cube, request)
        out[_GRID_KEY] = cube.grid
        return out

    def compute(self, inputs: dict[str, Any],
                request: Request) -> dict[str, Any]:
        """Default flow: stage_inputs -> run_model -> parse_outputs.

        Subclasses generally do NOT override this; override the three
        model-specific hooks instead. Override compute only if your
        model is in-process (no subprocess) and the stage-run-parse
        ceremony doesn't fit.
        """
        grid = inputs.pop(_GRID_KEY)
        keep = (self.keep_stage
                 or bool((request.context or {}).get("keep_stage")))
        stage = self._new_stage_dir()
        try:
            self.stage_inputs(grid, inputs, request, stage)
            output_path = self.run_model(stage, request)
            outputs = self.parse_outputs(output_path, grid)
            self._validate_outputs(outputs)
            return outputs
        finally:
            if not keep:
                shutil.rmtree(stage, ignore_errors=True)

    # ------------------------------------------------------------------
    # subclass hooks
    # ------------------------------------------------------------------
    @abstractmethod
    def stage_inputs(self, grid, inputs: dict[str, Any],
                     request: Request, stage_dir: Path) -> None:
        """Write `inputs` into the model's expected on-disk format.

        `grid` is the cube's SimulationGrid (height, width, pixel_m,
        crs_epsg, x0, y1). `inputs` is a dict from DataAdapter.fetch():
        static vars are ndarrays; time vars are (timestamps, ndarray);
        optional missing inputs are None. `request` carries t_start /
        t_end / context. `stage_dir` is a clean working directory
        unique to this invocation.
        """

    @abstractmethod
    def run_model(self, stage_dir: Path, request: Request) -> Path:
        """Invoke the model. Returns the path containing outputs.

        Typical implementation::

            subprocess.run([self.binary, ...], cwd=stage_dir, check=True)
            return stage_dir / "output.nc"

        Subprocess failures propagate; the engine's RetryPolicy can
        retry transient errors when configured.
        """

    @abstractmethod
    def parse_outputs(self, output_path: Path,
                      grid) -> dict[str, Any]:
        """Read model outputs and return arrays keyed by declared
        produce names. Each value must be shaped to match `grid` (for
        static outputs) or (T, H, W) for time outputs. Time outputs
        also need a timestamp axis — return it through
        ``request.context['t_axis']`` in `compute`, or override
        `update` if a richer write path is needed."""

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _new_stage_dir(self) -> Path:
        if self.stage_root is not None:
            self.stage_root.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(
                prefix=f"{self.name}_", dir=str(self.stage_root)))
        return Path(tempfile.mkdtemp(prefix=f"{self.name}_"))

    def _validate_outputs(self, outputs: dict[str, Any]) -> None:
        declared = {v.name for v in self.produces}
        returned = set(outputs.keys())
        missing = declared - returned
        if missing:
            raise RuntimeError(
                f"{self.name}.parse_outputs did not return "
                f"{sorted(missing)}; declared produces={sorted(declared)}")
        extra = returned - declared
        if extra:
            raise RuntimeError(
                f"{self.name}.parse_outputs returned undeclared keys "
                f"{sorted(extra)}; declared produces={sorted(declared)}")
