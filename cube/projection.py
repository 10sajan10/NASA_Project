"""Authoritative Stage-1 artifact projection into immutable Cube entries.

The values in this module are receipts, not write requests.  A caller cannot
choose their producer label, content digest, descriptor, or lineage: the
``CubeProjector`` reconstructs every field from the RuntimeStore's committed
graph, artifact commit, validation record, and immutable manifest.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from contracts.types import ArtifactDescriptor, TemporalKind
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_copy,
    strict_hash,
    strict_json_loads,
)
from engine.runtime.state import RuntimeStore
from engine.runtime.types import (
    ArtifactRecipe,
    BoundExecutionGraph,
    ScientificArtifactBinding,
)

from .catalog import Catalog
from .entries import CubeEntry, ProjectionAuthority, _mint_projection_authority, EntryInput


_HEX_LENGTH = 64


def _text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")


def _digest(value: str, label: str) -> None:
    _text(value, label)
    if (len(value) != _HEX_LENGTH
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class ProjectionInput:
    """One exact runtime artifact -> Cube entry lineage edge."""

    port: str
    source_run_id: str
    recipe_id: str
    artifact_id: str
    entry_id: str

    def __post_init__(self) -> None:
        _text(self.port, "projection input port")
        _text(self.source_run_id, "projection input source_run_id")
        _digest(self.recipe_id, "projection input recipe_id")
        _digest(self.artifact_id, "projection input artifact_id")
        _digest(self.entry_id, "projection input entry_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "port": self.port,
            "source_run_id": self.source_run_id,
            "recipe_id": self.recipe_id,
            "artifact_id": self.artifact_id,
            "entry_id": self.entry_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ProjectionInput":
        return cls(**require_object_fields(
            value, {
                "port", "source_run_id", "recipe_id", "artifact_id",
                "entry_id",
            },
            "ProjectionInput"))


@dataclass(frozen=True)
class CubeEntryProjection:
    """Content-addressed receipt for one authoritative Cube projection."""

    projection_id: str
    run_id: str
    runtime_plan_id: str
    bound_plan_id: str
    invocation_id: str
    output_port: str
    recipe_id: str
    artifact_id: str
    content_sha256: str
    descriptor_id: str
    descriptor: dict[str, Any]
    inputs: tuple[ProjectionInput, ...]
    entry_id: str

    def __post_init__(self) -> None:
        _digest(self.projection_id, "projection_id")
        _text(self.run_id, "projection run_id")
        for value, label in (
            (self.runtime_plan_id, "runtime_plan_id"),
            (self.bound_plan_id, "bound_plan_id"),
            (self.invocation_id, "invocation_id"),
            (self.recipe_id, "recipe_id"),
            (self.artifact_id, "artifact_id"),
            (self.content_sha256, "content_sha256"),
            (self.descriptor_id, "descriptor_id"),
            (self.entry_id, "entry_id"),
        ):
            _digest(value, label)
        _text(self.output_port, "projection output_port")
        if (not isinstance(self.inputs, tuple)
                or not all(isinstance(value, ProjectionInput)
                           for value in self.inputs)):
            raise TypeError("projection inputs must be typed ProjectionInput values")
        if self.inputs != tuple(sorted(self.inputs, key=lambda value: value.port)):
            raise ValueError("projection inputs must be in canonical port order")
        if len({value.port for value in self.inputs}) != len(self.inputs):
            raise ValueError("projection cannot bind one input port twice")
        descriptor = ArtifactDescriptor.from_dict(strict_copy(self.descriptor))
        object.__setattr__(self, "descriptor", descriptor.to_dict())
        if descriptor.descriptor_id != self.descriptor_id:
            raise ValueError("projection descriptor identity does not verify")
        if self.output_port == "":  # covered by _text; keeps the relation explicit
            raise ValueError("projection must name its output port")
        if self.entry().entry_id != self.entry_id:
            raise ValueError("projection Cube entry identity does not verify")
        if self.expected_id() != self.projection_id:
            raise ValueError("Cube projection identity does not verify")

    @classmethod
    def bind(
        cls,
        *,
        run_id: str,
        runtime_plan_id: str,
        binding: ScientificArtifactBinding,
        recipe_id: str,
        artifact_id: str,
        content_sha256: str,
        inputs: tuple[ProjectionInput, ...],
    ) -> "CubeEntryProjection":
        ordered = tuple(sorted(inputs, key=lambda value: value.port))
        descriptor = ArtifactDescriptor.from_dict(strict_copy(binding.descriptor))
        kind = ("static" if descriptor.temporal_support.kind
                is TemporalKind.TIME_INVARIANT else "time")
        entry = CubeEntry.create(
            concept=descriptor.concept_id,
            kind=kind,
            # This is the content-addressed BoundInvocation identity, never a
            # caller-supplied display string.
            producer=binding.invocation_id,
            content_sha256=content_sha256,
            grid=descriptor.grid,
            inputs=tuple(EntryInput(value.port, value.entry_id)
                         for value in ordered),
            run_id=run_id,
        )
        payload = {
            "schema": "cube-entry-projection-v1",
            "run_id": run_id,
            "runtime_plan_id": runtime_plan_id,
            "bound_plan_id": binding.bound_plan_id,
            "invocation_id": binding.invocation_id,
            "output_port": binding.output_port,
            "recipe_id": recipe_id,
            "artifact_id": artifact_id,
            "content_sha256": content_sha256,
            "descriptor_id": binding.descriptor_id,
            "descriptor": descriptor.to_dict(),
            "inputs": [value.to_dict() for value in ordered],
            "entry_id": entry.entry_id,
        }
        return cls(
            projection_id=strict_hash(payload),
            run_id=run_id,
            runtime_plan_id=runtime_plan_id,
            bound_plan_id=binding.bound_plan_id,
            invocation_id=binding.invocation_id,
            output_port=binding.output_port,
            recipe_id=recipe_id,
            artifact_id=artifact_id,
            content_sha256=content_sha256,
            descriptor_id=binding.descriptor_id,
            descriptor=descriptor.to_dict(),
            inputs=ordered,
            entry_id=entry.entry_id,
        )

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema": "cube-entry-projection-v1",
            "run_id": self.run_id,
            "runtime_plan_id": self.runtime_plan_id,
            "bound_plan_id": self.bound_plan_id,
            "invocation_id": self.invocation_id,
            "output_port": self.output_port,
            "recipe_id": self.recipe_id,
            "artifact_id": self.artifact_id,
            "content_sha256": self.content_sha256,
            "descriptor_id": self.descriptor_id,
            "descriptor": strict_copy(self.descriptor),
            "inputs": [value.to_dict() for value in self.inputs],
            "entry_id": self.entry_id,
        }

    def expected_id(self) -> str:
        return strict_hash(self.identity_payload())

    def to_dict(self) -> dict[str, Any]:
        return {"projection_id": self.projection_id, **self.identity_payload()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CubeEntryProjection":
        raw = require_object_fields(value, {
            "projection_id", "schema", "run_id", "runtime_plan_id",
            "bound_plan_id", "invocation_id", "output_port", "recipe_id",
            "artifact_id", "content_sha256", "descriptor_id", "descriptor",
            "inputs", "entry_id",
        }, "CubeEntryProjection")
        if raw.pop("schema") != "cube-entry-projection-v1":
            raise ValueError("unsupported Cube projection schema")
        if not isinstance(raw["inputs"], list):
            raise ValueError("Cube projection inputs must be an array")
        raw["inputs"] = tuple(
            ProjectionInput.from_dict(value) for value in raw["inputs"])
        return cls(**raw)

    def entry(self) -> CubeEntry:
        descriptor = ArtifactDescriptor.from_dict(strict_copy(self.descriptor))
        kind = ("static" if descriptor.temporal_support.kind
                is TemporalKind.TIME_INVARIANT else "time")
        return CubeEntry.create(
            concept=descriptor.concept_id,
            kind=kind,
            producer=self.invocation_id,
            content_sha256=self.content_sha256,
            grid=descriptor.grid,
            inputs=tuple(EntryInput(value.port, value.entry_id)
                         for value in self.inputs),
            run_id=self.run_id,
        )


class ProjectionFailpoint(str, Enum):
    BEFORE_CUBE_COMMIT = "before_cube_commit"
    AFTER_CUBE_COMMIT = "after_cube_commit"
    AFTER_OUTBOX_ACK = "after_outbox_ack"


class ProjectionDeferred(RuntimeError):
    """An upstream authoritative artifact has not been projected yet."""


Failpoint = Callable[[str], None]


class CubeProjector:
    """Replay RuntimeStore authority into a rebuildable DuckDB Cube view."""

    def __init__(
        self,
        runtime_root: Path | str,
        store: RuntimeStore,
        catalog: Catalog,
        *,
        failpoint: Failpoint | None = None,
    ) -> None:
        root = Path(runtime_root)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise ValueError("Cube projector runtime root must be an absolute directory")
        self.runtime_root = root.resolve()
        db_path = Path(store.db_path).resolve()
        try:
            db_path.relative_to(self.runtime_root)
        except ValueError as exc:
            raise ValueError("RuntimeStore is outside the projector runtime root") from exc
        self.store = store
        self.catalog = catalog
        self.failpoint = failpoint

    def project_pending(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Cube projection limit must be a positive integer")
        projected = 0
        # SQLite's PROJECTED bit is a delivery checkpoint, not an assertion
        # that every possible DuckDB target contains the entry.  Include every
        # unacknowledged row, plus globally-acknowledged rows absent from this
        # exact target catalog.  This makes a fresh catalog rebuildable from
        # RuntimeStore authority without resetting the global outbox.
        remaining: list[tuple[str, str, str]] = []
        with self.store.connect() as connection:
            authoritative = connection.execute(
                "SELECT run_id,recipe_id,artifact_id,state "
                "FROM cube_projection_outbox "
                "ORDER BY created_at,run_id,recipe_id",
            )
            for row in authoritative:
                key = (str(row[0]), str(row[1]), str(row[2]))
                try:
                    existing = self.catalog.projection_for_artifact(
                        key[0], key[2])
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "Cube target projection identity conflict") from exc
                if (row[3] != "PROJECTED" or existing is None
                        or existing.recipe_id != key[1]):
                    remaining.append(key)
                    if len(remaining) >= limit:
                        break
        while remaining:
            deferred: list[tuple[str, str, str]] = []
            progress = False
            for key in remaining:
                try:
                    projection, authority = self._reconstruct(*key)
                    self._hit(ProjectionFailpoint.BEFORE_CUBE_COMMIT)
                    entry = self.catalog._commit_authoritative_projection(
                        projection, authority)
                    self._hit(ProjectionFailpoint.AFTER_CUBE_COMMIT)
                    self._ack(projection, entry.entry_id)
                    self._hit(ProjectionFailpoint.AFTER_OUTBOX_ACK)
                    projected += 1
                    progress = True
                except ProjectionDeferred:
                    deferred.append(key)
                except BaseException as exc:
                    self._record_failure(key, exc)
                    raise
            if not progress:
                break
            remaining = deferred
        # Rows whose exact parents are not in this bounded batch remain
        # PENDING; a later call (or controller restart) continues safely.
        return projected

    def _reconstruct(
        self, run_id: str, recipe_id: str, artifact_id: str,
    ) -> tuple[CubeEntryProjection, ProjectionAuthority]:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT r.plan_id AS runtime_plan_id,b.graph_json,"
                "c.task_id,c.output_name,c.validation_id,"
                "c.attempt_id AS commit_attempt_id,"
                "a.recipe_id AS artifact_recipe_id,a.content_sha256,"
                "a.manifest_path,a.manifest_json,"
                "v.attempt_id AS validation_attempt_id,"
                "v.recipe_id AS validation_recipe_id,v.validator_id,"
                "v.content_sha256 AS validation_content_sha256,"
                "v.passed,v.report_json,p.recipe_json "
                "FROM cube_projection_outbox o "
                "JOIN artifact_commits c ON c.run_id=o.run_id "
                " AND c.recipe_id=o.recipe_id AND c.artifact_id=o.artifact_id "
                "JOIN runs r ON r.run_id=o.run_id "
                "JOIN bound_graphs b ON b.plan_id=r.plan_id "
                "JOIN artifacts a ON a.artifact_id=o.artifact_id "
                "JOIN validation_records v ON v.validation_id=c.validation_id "
                "JOIN artifact_recipes p ON p.recipe_id=o.recipe_id "
                "WHERE o.run_id=? AND o.recipe_id=? AND o.artifact_id=?",
                (run_id, recipe_id, artifact_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("Cube outbox row lacks an authoritative artifact commit")

        graph = BoundExecutionGraph.from_dict(strict_json_loads(row["graph_json"]))
        if graph.plan_id != row["runtime_plan_id"]:
            raise RuntimeError("runtime graph identity changed before Cube projection")
        task = graph.task_by_id(row["task_id"])
        try:
            graph_recipe = next(value for value in task.outputs
                                if value.output_name == row["output_name"])
        except StopIteration as exc:
            raise RuntimeError("artifact commit names an undeclared output") from exc
        recipe = ArtifactRecipe.from_dict(strict_json_loads(row["recipe_json"]))
        if (recipe != graph_recipe or recipe.recipe_id != recipe_id
                or recipe.output_name != row["output_name"]
                or recipe.scientific_binding is None):
            raise RuntimeError(
                "Cube projection recipe is absent, stale, or not scientifically bound")
        binding = recipe.scientific_binding

        if (row["artifact_recipe_id"] != recipe_id
                or row["validation_recipe_id"] != recipe_id
                or row["content_sha256"]
                != row["validation_content_sha256"]
                or row["commit_attempt_id"] != row["validation_attempt_id"]
                or int(row["passed"]) != 1):
            raise RuntimeError("artifact validation is not bound to this commit")
        report = strict_json_loads(row["report_json"])
        expected_binding = binding.to_dict()
        if (not isinstance(report, dict)
                or report.get("schema") != "stage1-validation-record-v2"
                or report.get("passed") is not True
                or report.get("recipe_id") != recipe_id
                or report.get("content_sha256") != row["content_sha256"]
                or report.get("validator_id") != row["validator_id"]
                or report.get("validator_configuration")
                != strict_copy(recipe.validation)
                or report.get("scientific_binding") != expected_binding):
            raise RuntimeError(
                "validation record did not replay the scientific output binding")
        expected_validation_id = strict_hash({
            "schema": "stage1-validation-record-v2",
            "attempt_id": row["validation_attempt_id"],
            "recipe_id": recipe_id,
            "validator_id": row["validator_id"],
            "content_sha256": row["content_sha256"],
            "report": report,
        })
        if expected_validation_id != row["validation_id"]:
            raise RuntimeError("validation receipt identity does not verify")

        self._verify_manifest(
            manifest_path=Path(row["manifest_path"]),
            manifest_json=row["manifest_json"],
            expected_artifact_id=artifact_id,
            expected_recipe_id=recipe_id,
            expected_content_sha256=row["content_sha256"],
        )

        inputs = self._projection_inputs(run_id, graph, task.task_id)
        projection = CubeEntryProjection.bind(
            run_id=run_id,
            runtime_plan_id=graph.plan_id,
            binding=binding,
            recipe_id=recipe_id,
            artifact_id=artifact_id,
            content_sha256=row["content_sha256"],
            inputs=inputs,
        )
        # Mint only after the manifest/object bytes and every projection
        # coordinate have both been replayed.  The authority is therefore not
        # transferable to another self-consistent receipt for the same bytes.
        authority = _mint_projection_authority(
            projection.projection_id,
            strict_canonical_json(projection.to_dict()),
        )
        return projection, authority

    def _projection_inputs(
        self, run_id: str, graph: BoundExecutionGraph, task_id: str,
    ) -> tuple[ProjectionInput, ...]:
        task = graph.task_by_id(task_id)
        task_by_key = {value.key: value for value in graph.tasks}
        values: list[ProjectionInput] = []
        with self.store.connect() as connection:
            for binding in sorted(task.inputs, key=lambda value: value.input_name):
                upstream = task_by_key[binding.upstream_task]
                upstream_recipe = next(
                    value for value in upstream.outputs
                    if value.output_name == binding.upstream_output)
                row = connection.execute(
                    "SELECT s.recipe_id,s.artifact_id FROM task_output_slots s "
                    "JOIN artifact_commits c ON c.run_id=s.run_id "
                    "AND c.recipe_id=s.recipe_id AND c.artifact_id=s.artifact_id "
                    "AND c.task_id=s.task_id AND c.output_name=s.output_name "
                    "WHERE s.run_id=? AND s.task_id=? AND s.output_name=?",
                    (run_id, upstream.task_id, binding.upstream_output),
                ).fetchone()
                if row is None:
                    raise RuntimeError(
                        "scientific output committed without its exact input artifact")
                if row[0] != upstream_recipe.recipe_id:
                    raise RuntimeError(
                        "scientific input artifact does not match its bound recipe")
                parent = self.catalog.projection_for_artifact(run_id, row[1])
                if parent is None:
                    raise ProjectionDeferred(
                        f"upstream artifact {row[1]} is not projected")
                if parent.recipe_id != row[0]:
                    raise RuntimeError("Cube lineage parent recipe does not match runtime")
                values.append(ProjectionInput(
                    binding.input_name, run_id, row[0], row[1],
                    parent.entry_id))
            for binding in sorted(
                    task.external_inputs, key=lambda value: value.input_name):
                row = connection.execute(
                    "SELECT e.source_run_id,e.recipe_id,e.artifact_id "
                    "FROM task_external_inputs e "
                    "JOIN artifact_commits c ON c.run_id=e.source_run_id "
                    "AND c.recipe_id=e.recipe_id AND c.artifact_id=e.artifact_id "
                    "WHERE e.run_id=? AND e.task_id=? AND e.input_name=?",
                    (run_id, task.task_id, binding.input_name),
                ).fetchone()
                if row is None or row[2] != binding.artifact_id:
                    raise RuntimeError(
                        "scientific external input does not match its frozen "
                        "authoritative artifact binding")
                parent = self.catalog.projection_for_artifact(row[0], row[2])
                if parent is None:
                    raise ProjectionDeferred(
                        f"external artifact {row[2]} is not projected")
                if parent.recipe_id != row[1]:
                    raise RuntimeError(
                        "external Cube lineage parent recipe does not match runtime")
                values.append(ProjectionInput(
                    binding.input_name, row[0], row[1], row[2],
                    parent.entry_id))
        return tuple(values)

    def _verify_manifest(
        self, *, manifest_path: Path, manifest_json: str,
        expected_artifact_id: str, expected_recipe_id: str,
        expected_content_sha256: str,
    ) -> None:
        if not manifest_path.is_absolute():
            raise RuntimeError("authoritative artifact manifest path is not absolute")
        if manifest_path.is_symlink():
            raise RuntimeError("artifact manifest cannot be a symbolic link")
        resolved = manifest_path.resolve()
        self._beneath(resolved, "artifact manifest")
        if not resolved.is_file():
            raise RuntimeError("artifact manifest is not a regular immutable file")
        stored = strict_json_loads(manifest_json)
        manifest_bytes = resolved.read_bytes()
        on_disk = strict_json_loads(manifest_bytes)
        if (stored != on_disk
                or manifest_json != strict_canonical_json(stored)
                or manifest_bytes != manifest_json.encode("ascii")):
            raise RuntimeError("artifact manifest file differs from RuntimeStore")
        expected_fields = {
            "schema", "artifact_id", "recipe_id", "media_type",
            "content_sha256", "size_bytes", "object_path",
        }
        if (not isinstance(stored, dict) or set(stored) != expected_fields
                or stored["schema"] != "stage1-artifact-manifest-v1"
                or stored["artifact_id"] != expected_artifact_id
                or stored["recipe_id"] != expected_recipe_id
                or stored["media_type"] != "application/json"
                or stored["content_sha256"] != expected_content_sha256
                or isinstance(stored["size_bytes"], bool)
                or not isinstance(stored["size_bytes"], int)
                or stored["size_bytes"] < 0
                or not isinstance(stored["object_path"], str)
                or not stored["object_path"]):
            raise RuntimeError("authoritative artifact manifest identity is invalid")
        expected_manifest_artifact_id = strict_hash({
            "schema": "stage1-artifact-identity-v1",
            "recipe_id": expected_recipe_id,
            "media_type": stored["media_type"],
            "content_sha256": expected_content_sha256,
            "size_bytes": stored["size_bytes"],
        })
        if expected_manifest_artifact_id != expected_artifact_id:
            raise RuntimeError("artifact identity does not replay from its manifest")
        raw_object = Path(stored["object_path"])
        object_candidate = (raw_object if raw_object.is_absolute()
                            else self.runtime_root / raw_object)
        if object_candidate.is_symlink():
            raise RuntimeError("artifact object cannot be a symbolic link")
        object_path = object_candidate.resolve()
        self._beneath(object_path, "artifact object")
        if not object_path.is_file():
            raise RuntimeError("artifact object is not a regular immutable file")
        digest = hashlib.sha256()
        size = 0
        with object_path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
        if (digest.hexdigest() != expected_content_sha256
                or size != stored["size_bytes"]):
            raise RuntimeError("artifact object no longer matches its manifest")
        # Authority is minted by the caller only after it also constructs the
        # complete canonical projection that these verified bytes will back.

    def _ack(self, projection: CubeEntryProjection, entry_id: str) -> None:
        with self.store.transaction() as connection:
            authority = connection.execute(
                "SELECT 1 FROM artifact_commits WHERE run_id=? AND recipe_id=? "
                "AND artifact_id=?",
                (projection.run_id, projection.recipe_id,
                 projection.artifact_id),
            ).fetchone()
            if authority is None:
                raise RuntimeError("artifact authority disappeared before outbox ack")
            changed = connection.execute(
                "UPDATE cube_projection_outbox SET state='PROJECTED',"
                "attempts=attempts+1,error=NULL,projection_id=?,entry_id=?,"
                "projected_at=? WHERE run_id=? AND recipe_id=? AND artifact_id=? "
                "AND state IN ('PENDING','FAILED','PROJECTED')",
                (projection.projection_id, entry_id,
                 __import__("time").time(), projection.run_id,
                 projection.recipe_id, projection.artifact_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Cube projection outbox identity changed")

    def _record_failure(
        self, key: tuple[str, str, str], error: BaseException,
    ) -> None:
        # Failpoints model process death and must leave PENDING unchanged.
        if isinstance(error, BaseException) and not isinstance(error, Exception):
            return
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE cube_projection_outbox SET state='FAILED',"
                "attempts=attempts+1,error=? WHERE run_id=? AND recipe_id=? "
                "AND artifact_id=? AND state!='PROJECTED'",
                (f"{type(error).__name__}: {error}", *key),
            )

    def _beneath(self, value: Path, label: str) -> None:
        try:
            value.relative_to(self.runtime_root)
        except ValueError as exc:
            raise RuntimeError(f"{label} escapes the runtime root") from exc

    def _hit(self, point: ProjectionFailpoint) -> None:
        if self.failpoint is not None:
            self.failpoint(point.value)


__all__ = [
    "CubeEntryProjection",
    "CubeProjector",
    "ProjectionDeferred",
    "ProjectionFailpoint",
    "ProjectionInput",
]
