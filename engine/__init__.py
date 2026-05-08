"""Generic orchestration engine.

Decouples three concerns the existing pipeline mixes together:

  * Storage    -> the Cube (already in `cube/`)
  * Execution  -> backends (Serial / Thread / Process / Dask / SLURM)
  * Adapters   -> ProducerV2 contract (extract -> compute -> update)

Lives alongside `fusion/`. The legacy resolver continues to work; producers
migrate to the new contract one at a time.
"""
from .contracts import (
    CostHint,
    MergePolicy,
    ProducerCapabilities,
    ProducerV2,
    Request,
    TileSpec,
    VarSpec,
)
from .backends import (
    Backend,
    DaskBackend,
    ProcessBackend,
    SerialBackend,
    ThreadBackend,
    make_backend,
)
from .spill import spill_array, workspace
from .manifest import RunManifest
from .config import load_config, merge_overrides
from .contract_test import (
    FakeCube,
    check_capabilities,
    check_idempotency,
    check_io_isolation,
    check_producer,
)

__all__ = [
    "CostHint",
    "MergePolicy",
    "ProducerCapabilities",
    "ProducerV2",
    "Request",
    "TileSpec",
    "VarSpec",
    "Backend",
    "DaskBackend",
    "ProcessBackend",
    "SerialBackend",
    "ThreadBackend",
    "make_backend",
    "spill_array",
    "workspace",
    "RunManifest",
    "load_config",
    "merge_overrides",
    "FakeCube",
    "check_capabilities",
    "check_idempotency",
    "check_io_isolation",
    "check_producer",
]
