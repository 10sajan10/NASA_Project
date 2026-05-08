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
from .schema import (
    CURRENT_SCHEMA_VERSION,
    ensure_schema,
    get_schema_version,
    register_migration,
)
from .checkpoint import CheckpointStore
from .config import load_config, merge_overrides
from .contract_test import (
    FakeCube,
    check_capabilities,
    check_idempotency,
    check_io_isolation,
    check_producer,
)
from .registry import (
    ProducerRegistry,
    producer_produces,
    producer_requires,
    to_engine_registry,
)
from .cube_ref import CubeRef, is_cross_process_backend
from .tiled import TiledProducer, is_tile_aware
from .merge import (
    merge as merge_arrays,
    write_chunk_static_with_policy,
    write_chunk_time_with_policy,
)
from .pipeline import Pipeline, PipelineNode, Trigger, parallel
from .scheduler import PipelineRunner, RunResult, StepResult

__all__ = [
    # contracts
    "CostHint",
    "MergePolicy",
    "ProducerCapabilities",
    "ProducerV2",
    "Request",
    "TileSpec",
    "VarSpec",
    # backends
    "Backend",
    "DaskBackend",
    "ProcessBackend",
    "SerialBackend",
    "ThreadBackend",
    "make_backend",
    # spill
    "spill_array",
    "workspace",
    # manifest + schema + checkpoint
    "RunManifest",
    "CURRENT_SCHEMA_VERSION",
    "ensure_schema",
    "get_schema_version",
    "register_migration",
    "CheckpointStore",
    # config
    "load_config",
    "merge_overrides",
    # contract tests
    "FakeCube",
    "check_capabilities",
    "check_idempotency",
    "check_io_isolation",
    "check_producer",
    # registry + pipeline + runner
    "ProducerRegistry",
    "producer_produces",
    "producer_requires",
    "to_engine_registry",
    "Pipeline",
    "PipelineNode",
    "Trigger",
    "parallel",
    "PipelineRunner",
    "RunResult",
    "StepResult",
    # cube cross-process
    "CubeRef",
    "is_cross_process_backend",
    # tile-level fan-out
    "TiledProducer",
    "is_tile_aware",
    # merge policies
    "merge_arrays",
    "write_chunk_static_with_policy",
    "write_chunk_time_with_policy",
]
