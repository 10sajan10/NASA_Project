"""Validated, immutable, fenced artifact publication for the Stage-1 runtime.

Workers write only to an attempt-scoped staging directory.  This module is the
controller-owned boundary which snapshots those bytes into an immutable object
store, validates the snapshot, and makes a complete task result visible in one
SQLite transaction.  A file existing in ``objects/`` or ``manifests/`` is not
publication: only ``task_commits`` plus ``task_output_slots`` is authoritative.

The implementation supports one JSON payload file per output port.  In
addition to the Stage-1 ``finite_json`` validator, Stage 4 adds a deliberately
narrow ``field_json_v2`` validator for the canonical, domain-neutral test-field
representation.  New scientific formats must add explicit validators; they
must not bypass this boundary.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from grid_convention import (
    FIELD_JSON_SCHEMA,
    FIELD_JSON_VALIDATOR_KIND,
    GRID_AFFINE_CONVENTION,
)

from .identity import strict_canonical_json, strict_hash
from .native import NATIVE_FILE_POINTER_VALIDATOR_KIND, NativeFilePointer
from .state import AttemptRecord, RuntimeStore
from .types import ArtifactRecipe, AttemptSpec, AttemptState, TaskState


_SAFE_PORT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RESULT_SCHEMA = "stage1-attempt-result-v1"
_ARTIFACT_SCHEMA = "stage1-artifact-manifest-v1"
_INVENTORY_SCHEMA = "stage1-staged-inventory-v1"
_VALIDATION_SCHEMA = "stage1-validation-record-v2"
_MAX_RESULT_BYTES = 1024 * 1024
_READ_CHUNK = 1024 * 1024


# Stable crash boundaries used by recovery tests.  A failpoint callable may
# raise at any of these names; SQLite transactions roll back BaseException too.
AFTER_RESULT_PARSED = "after_result_parsed"
AFTER_OBJECTS_FINALIZED = "after_objects_finalized"
AFTER_MANIFESTS_FINALIZED = "after_manifests_finalized"
AFTER_VALIDATION_RECORDED = "after_validation_recorded"
AFTER_BEGIN_COMMITTING = "after_begin_committing"
BEFORE_AUTHORITATIVE_COMMIT = "before_authoritative_commit"
INSIDE_AUTHORITATIVE_COMMIT = "inside_authoritative_commit"
AFTER_AUTHORITATIVE_COMMIT = "after_authoritative_commit"
FAILPOINTS = (
    AFTER_RESULT_PARSED,
    AFTER_OBJECTS_FINALIZED,
    AFTER_MANIFESTS_FINALIZED,
    AFTER_VALIDATION_RECORDED,
    AFTER_BEGIN_COMMITTING,
    BEFORE_AUTHORITATIVE_COMMIT,
    INSIDE_AUTHORITATIVE_COMMIT,
    AFTER_AUTHORITATIVE_COMMIT,
)


class CommitDisposition(str, Enum):
    """Outcome of presenting one provider result to the committer."""

    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    STALE = "STALE"
    INVALID = "INVALID"
    CONFLICT = "CONFLICT"


class ArtifactError(RuntimeError):
    """Base class for artifact-boundary failures."""


class InvalidArtifactError(ArtifactError):
    """The staged result does not satisfy its immutable output contract."""


class ArtifactConflictError(ArtifactError):
    """An immutable identity was presented with different bytes or metadata."""


@dataclass(frozen=True)
class StagedArtifact:
    attempt_id: str
    recipe: ArtifactRecipe
    source_path: Path
    object_path: Path
    object_relative_path: str
    content_sha256: str
    size_bytes: int
    inventory: dict[str, Any]


@dataclass(frozen=True)
class ValidationRecord:
    validation_id: str
    attempt_id: str
    recipe_id: str
    validator_id: str
    content_sha256: str
    passed: bool
    report: dict[str, Any]


@dataclass(frozen=True)
class ArtifactManifest:
    artifact_id: str
    recipe_id: str
    output_name: str
    content_sha256: str
    size_bytes: int
    object_path: str
    manifest_path: Path
    value: dict[str, Any]


Failpoint = Callable[[str], None]


class ArtifactCommitter:
    """Finalize and atomically commit all outputs of one current attempt.

    ``runtime_root`` and the SQLite database must share the validated local
    runtime root.  Stage 1 provides same-node process/controller-crash recovery;
    this class makes no node-loss durability claim.
    """

    def __init__(self, runtime_root: Path | str, store: RuntimeStore, *,
                 failpoint: Failpoint | None = None) -> None:
        raw_root = Path(runtime_root)
        if not raw_root.is_absolute() or not raw_root.is_dir():
            raise ValueError("artifact runtime root must be an existing absolute directory")
        if raw_root.is_symlink():
            raise ValueError("artifact runtime root cannot be a symbolic link")
        self.runtime_root = raw_root.resolve()
        self.store = store
        self.failpoint = failpoint

        db_path = Path(store.db_path)
        if not db_path.is_absolute():
            db_path = db_path.absolute()
        self._require_beneath(db_path, "runtime database", may_not_exist=True)

        self.objects_root = self.runtime_root / "objects" / "sha256"
        self.manifests_root = self.runtime_root / "manifests" / "sha256"
        self.incoming_root = self.runtime_root / "objects" / ".incoming"
        for directory in (
                self.objects_root, self.manifests_root, self.incoming_root):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"unsafe controller-owned directory: {directory}")

    def process(self, record: AttemptRecord) -> CommitDisposition:
        """Validate and commit ``record`` or classify it without publication.

        This method is idempotent across controller restart.  Immutable objects
        may be finalized more than once, but the fenced SQLite transaction has
        one winner and is the only dependency-release point.
        """
        spec = record.spec
        try:
            preliminary = self._preflight(spec)
        except ArtifactConflictError as exc:
            # A conflicting immutable identity is not retryable.  Persist a
            # terminal outcome when this is still the current attempt so the
            # controller cannot spin forever in VALIDATING/COMMITTING.
            self._mark_invalid(spec, str(exc))
            return CommitDisposition.CONFLICT
        if preliminary is not None:
            return preliminary

        try:
            sources = self._result_sources(spec)
            self._hit(AFTER_RESULT_PARSED)
            staged = tuple(
                self._finalize_payload(spec, recipe, sources[recipe.output_name])
                for recipe in sorted(
                    spec.task.outputs, key=lambda value: value.output_name)
            )
            self._hit(AFTER_OBJECTS_FINALIZED)
            validations = tuple(self._validate(spec, value) for value in staged)

            if not all(value.passed for value in validations):
                self._record_prepared(staged, validations, state="REJECTED")
                message = "; ".join(
                    str(value.report.get("error", "validation failed"))
                    for value in validations if not value.passed
                )
                self._mark_invalid(spec, message or "output validation failed")
                return CommitDisposition.INVALID

            manifests = tuple(
                self._publish_manifest(value) for value in staged
            )
            self._hit(AFTER_MANIFESTS_FINALIZED)
            self._record_prepared(staged, validations, state="VALIDATED")
            self._hit(AFTER_VALIDATION_RECORDED)
        except InvalidArtifactError as exc:
            self._mark_invalid(spec, str(exc))
            return CommitDisposition.INVALID
        except ArtifactConflictError as exc:
            self._mark_invalid(spec, str(exc))
            return CommitDisposition.CONFLICT

        # This is intentionally a separate durable transition.  Recovery from
        # COMMITTING repeats immutable finalization and resumes the conditional
        # transaction below.
        try:
            if self._task_state(spec) is not TaskState.SUCCEEDED:
                self.store.begin_committing(spec.run_id, spec.task.task_id)
                self._hit(AFTER_BEGIN_COMMITTING)
            self._hit(BEFORE_AUTHORITATIVE_COMMIT)
            disposition = self._authoritative_commit(
                spec, staged, validations, manifests)
        except InvalidArtifactError as exc:
            self._mark_invalid(spec, str(exc))
            return CommitDisposition.INVALID
        except ArtifactConflictError as exc:
            self._mark_invalid(spec, str(exc))
            return CommitDisposition.CONFLICT
        self._hit(AFTER_AUTHORITATIVE_COMMIT)
        return disposition

    # ------------------------------------------------------------------
    # Staged-result parsing and confinement
    # ------------------------------------------------------------------
    def _result_sources(self, spec: AttemptSpec) -> dict[str, Path]:
        stage = self._safe_stage(spec.stage_dir)
        with self.store.connect() as con:
            row = con.execute(
                "SELECT result_manifest_path FROM attempts WHERE attempt_id=?",
                (spec.attempt_id,),
            ).fetchone()
        if row is None or not row[0]:
            raise InvalidArtifactError("attempt has no persisted result manifest")

        result_path = self._safe_existing_file(
            Path(row[0]), stage, "result manifest")
        expected_result = stage / "result.json"
        if result_path != expected_result:
            raise InvalidArtifactError(
                "result manifest must be the attempt root result.json")
        result = _read_strict_json(result_path, max_bytes=_MAX_RESULT_BYTES)
        # ``peak_memory_kb`` is telemetry, not part of the scientific result:
        # it is optional so an older attempt receipt still validates, and it is
        # never allowed to influence artifact identity or commit.
        if not isinstance(result, dict) or not (
                {"schema", "attempt_id", "attempt_token", "outputs"}
                <= set(result)
                <= {"schema", "attempt_id", "attempt_token", "outputs",
                    "peak_memory_kb"}):
            raise InvalidArtifactError("attempt result has an invalid field set")
        if "peak_memory_kb" in result and (
                isinstance(result["peak_memory_kb"], bool)
                or not isinstance(result["peak_memory_kb"], int)
                or result["peak_memory_kb"] < 0):
            raise InvalidArtifactError(
                "attempt result peak_memory_kb must be a non-negative integer")
        if (result["schema"] != _RESULT_SCHEMA
                or result["attempt_id"] != spec.attempt_id
                or result["attempt_token"] != spec.attempt_token):
            raise InvalidArtifactError("attempt result identity does not match its lease")

        outputs = result["outputs"]
        if not isinstance(outputs, dict):
            raise InvalidArtifactError("attempt result outputs must be an object")
        declared = {recipe.output_name for recipe in spec.task.outputs}
        if set(outputs) != declared:
            raise InvalidArtifactError(
                f"result ports {sorted(outputs)!r} do not exactly match "
                f"declared ports {sorted(declared)!r}")

        sources: dict[str, Path] = {}
        expected_files: set[Path] = set()
        for port in sorted(declared):
            if not _SAFE_PORT.fullmatch(port) or port in {".", ".."}:
                raise InvalidArtifactError(f"unsafe declared output port {port!r}")
            relative = Path("outputs") / port / "payload.json"
            entry = outputs[port]
            if not isinstance(entry, dict) or entry != {"path": relative.as_posix()}:
                raise InvalidArtifactError(
                    f"result entry for {port!r} must name {relative.as_posix()!r}")
            source = self._safe_existing_file(
                stage / relative, stage, f"output {port!r}")
            sources[port] = source
            expected_files.add(source)

        self._verify_exact_output_tree(stage, declared, expected_files)
        return sources

    def _safe_stage(self, raw: str | Path) -> Path:
        stage = self._require_beneath(Path(raw), "attempt staging directory")
        if not stage.is_dir():
            raise InvalidArtifactError("attempt staging path is not a directory")
        self._reject_symlink_components(stage, self.runtime_root)
        return stage

    def _safe_existing_file(self, raw: Path, confinement_root: Path,
                            label: str) -> Path:
        path = raw if raw.is_absolute() else confinement_root / raw
        lexical = Path(os.path.abspath(os.fspath(path)))
        try:
            lexical.relative_to(confinement_root)
        except ValueError as exc:
            raise InvalidArtifactError(f"{label} escapes attempt staging") from exc
        self._reject_symlink_components(lexical, confinement_root)
        try:
            info = lexical.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise InvalidArtifactError(f"{label} is missing") from exc
        if not stat.S_ISREG(info.st_mode):
            raise InvalidArtifactError(f"{label} is not a regular file")
        return lexical

    def _verify_exact_output_tree(self, stage: Path, ports: set[str],
                                  expected_files: set[Path]) -> None:
        outputs_root = stage / "outputs"
        self._reject_symlink_components(outputs_root, stage)
        if not outputs_root.is_dir():
            raise InvalidArtifactError("attempt outputs directory is missing")
        actual_ports: set[str] = set()
        actual_files: set[Path] = set()
        with os.scandir(outputs_root) as entries:
            for entry in entries:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    raise InvalidArtifactError(
                        f"unexpected non-directory output entry {entry.name!r}")
                actual_ports.add(entry.name)
                port_root = outputs_root / entry.name
                with os.scandir(port_root) as port_entries:
                    for child in port_entries:
                        child_path = port_root / child.name
                        if child.is_symlink() or not child.is_file(
                                follow_symlinks=False):
                            raise InvalidArtifactError(
                                f"unsafe entry in output {entry.name!r}: {child.name!r}")
                        actual_files.add(child_path)
        if actual_ports != ports or actual_files != expected_files:
            raise InvalidArtifactError(
                "attempt output tree contains missing or undeclared entries")

    # ------------------------------------------------------------------
    # Immutable object finalization and validation
    # ------------------------------------------------------------------
    def _finalize_payload(self, spec: AttemptSpec, recipe: ArtifactRecipe,
                          source: Path) -> StagedArtifact:
        temporary = self.incoming_root / f"payload-{uuid.uuid4().hex}.tmp"
        source_fd = _open_regular_readonly(source)
        tmp_fd: int | None = None
        try:
            tmp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
            )
            digest = hashlib.sha256()
            size = 0
            while True:
                block = os.read(source_fd, _READ_CHUNK)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                _write_all(tmp_fd, block)
            os.fsync(tmp_fd)
            os.fchmod(tmp_fd, 0o444)
            os.close(tmp_fd)
            tmp_fd = None
            content_sha256 = digest.hexdigest()
            relative = Path("objects") / "sha256" / content_sha256[:2] / content_sha256
            target = self.runtime_root / relative
            self._publish_temporary(
                temporary, target,
                expected_sha256=content_sha256, expected_size=size)
        except BaseException:
            if tmp_fd is not None:
                os.close(tmp_fd)
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(source_fd)

        inventory = {
            "schema": _INVENTORY_SCHEMA,
            "files": [{
                "path": "payload.json",
                "size_bytes": size,
                "sha256": content_sha256,
            }],
            "content_sha256": content_sha256,
        }
        return StagedArtifact(
            attempt_id=spec.attempt_id,
            recipe=recipe,
            source_path=source,
            object_path=target,
            object_relative_path=relative.as_posix(),
            content_sha256=content_sha256,
            size_bytes=size,
            inventory=inventory,
        )

    def _validate(self, spec: AttemptSpec,
                  staged: StagedArtifact) -> ValidationRecord:
        configuration = staged.recipe.validation
        validator_id = "stage1.finite-json@1"
        passed = False
        checksum_passed = False
        strict_json_passed = False
        field_validation_passed: bool | None = None
        native_pointer_passed: bool | None = None
        error: str | None = None
        decoded_type: str | None = None
        try:
            if staged.recipe.media_type != "application/json":
                raise InvalidArtifactError(
                    "JSON validation requires application/json")
            if configuration == {"kind": "finite_json"}:
                validator_id = "stage1.finite-json@1"
            elif (isinstance(configuration, dict)
                  and configuration.get("kind") == FIELD_JSON_VALIDATOR_KIND):
                validator_id = "stage8r.field-json@2"
                _validate_field_configuration(configuration)
                field_validation_passed = False
            elif configuration == {
                    "kind": NATIVE_FILE_POINTER_VALIDATOR_KIND}:
                validator_id = "stage10c.native-file-pointer@1"
                native_pointer_passed = False
            else:
                validator_id = "stage1.unsupported-validator@1"
                raise InvalidArtifactError(
                    f"unsupported validation contract for "
                    f"{staged.recipe.output_name!r}")
            payload = _read_verified_bytes(
                staged.object_path,
                staged.content_sha256,
                staged.size_bytes,
            )
            checksum_passed = True
            value = _decode_strict_json(payload)
            strict_json_passed = True
            decoded_type = type(value).__name__
            if configuration.get("kind") == FIELD_JSON_VALIDATOR_KIND:
                _validate_field_json_v2(value, configuration)
                field_validation_passed = True
            elif configuration.get("kind") == \
                    NATIVE_FILE_POINTER_VALIDATOR_KIND:
                pointer = NativeFilePointer.from_dict(value)
                pointer.verify_file()
                native_pointer_passed = True
            passed = True
        except (InvalidArtifactError, UnicodeError, json.JSONDecodeError,
                OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"

        report: dict[str, Any] = {
            "schema": _VALIDATION_SCHEMA,
            "validator_id": validator_id,
            "validator_configuration": configuration,
            # This is the exact compiler-bound descriptor and plan/output
            # coordinate, not metadata inferred from the payload.  The Cube
            # projector requires an identical replay in the passed validation
            # record before it will expose an entry.
            "scientific_binding": (
                staged.recipe.scientific_binding.to_dict()
                if staged.recipe.scientific_binding is not None else None),
            "recipe_id": staged.recipe.recipe_id,
            "content_sha256": staged.content_sha256,
            "size_bytes": staged.size_bytes,
            "checks": {
                "immutable_object_checksum": checksum_passed,
                "strict_finite_json": strict_json_passed,
                "field_json_v2": field_validation_passed,
                "native_file_pointer_v1": native_pointer_passed,
            },
            "passed": passed,
        }
        if decoded_type is not None:
            report["decoded_type"] = decoded_type
        if error is not None:
            report["error"] = error
        identity = {
            "schema": _VALIDATION_SCHEMA,
            "attempt_id": spec.attempt_id,
            "recipe_id": staged.recipe.recipe_id,
            "validator_id": validator_id,
            "content_sha256": staged.content_sha256,
            "report": report,
        }
        return ValidationRecord(
            validation_id=strict_hash(identity),
            attempt_id=spec.attempt_id,
            recipe_id=staged.recipe.recipe_id,
            validator_id=validator_id,
            content_sha256=staged.content_sha256,
            passed=passed,
            report=report,
        )

    def _publish_manifest(self, staged: StagedArtifact) -> ArtifactManifest:
        artifact_id = strict_hash({
            "schema": "stage1-artifact-identity-v1",
            "recipe_id": staged.recipe.recipe_id,
            "media_type": staged.recipe.media_type,
            "content_sha256": staged.content_sha256,
            "size_bytes": staged.size_bytes,
        })
        value = {
            "schema": _ARTIFACT_SCHEMA,
            "artifact_id": artifact_id,
            "recipe_id": staged.recipe.recipe_id,
            "media_type": staged.recipe.media_type,
            "content_sha256": staged.content_sha256,
            "size_bytes": staged.size_bytes,
            "object_path": staged.object_relative_path,
        }
        encoded = strict_canonical_json(value).encode("ascii")
        manifest_path = (
            self.manifests_root / artifact_id[:2] / f"{artifact_id}.json")
        self._publish_bytes(encoded, manifest_path)
        return ArtifactManifest(
            artifact_id=artifact_id,
            recipe_id=staged.recipe.recipe_id,
            output_name=staged.recipe.output_name,
            content_sha256=staged.content_sha256,
            size_bytes=staged.size_bytes,
            object_path=staged.object_relative_path,
            manifest_path=manifest_path,
            value=value,
        )

    def _publish_bytes(self, payload: bytes, target: Path) -> None:
        temporary = self.incoming_root / f"manifest-{uuid.uuid4().hex}.tmp"
        fd: int | None = None
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
            )
            _write_all(fd, payload)
            os.fsync(fd)
            os.fchmod(fd, 0o444)
            os.close(fd)
            fd = None
            self._publish_temporary(
                temporary, target,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                expected_size=len(payload),
            )
        except BaseException:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)
            raise

    def _publish_temporary(self, temporary: Path, target: Path, *,
                           expected_sha256: str, expected_size: int) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink():
            raise ArtifactConflictError("immutable-object directory is a symlink")
        try:
            os.link(temporary, target, follow_symlinks=False)
            _fsync_dir(target.parent)
        except FileExistsError:
            try:
                _read_verified_bytes(target, expected_sha256, expected_size)
            except (InvalidArtifactError, OSError) as exc:
                raise ArtifactConflictError(
                    f"immutable path conflicts with expected content: {target}") from exc
        finally:
            temporary.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Durable validation records and authoritative commit
    # ------------------------------------------------------------------
    def _record_prepared(self, staged: tuple[StagedArtifact, ...],
                         validations: tuple[ValidationRecord, ...], *,
                         state: str) -> None:
        by_recipe = {value.recipe_id: value for value in validations}
        with self.store.transaction() as con:
            for value in staged:
                inventory_json = strict_canonical_json(value.inventory)
                expected = (
                    state, str(value.source_path), inventory_json,
                    value.content_sha256,
                )
                existing = con.execute(
                    "SELECT state,staging_path,inventory_json,content_sha256 "
                    "FROM staged_artifacts WHERE attempt_id=? AND recipe_id=?",
                    (value.attempt_id, value.recipe.recipe_id),
                ).fetchone()
                if existing is None:
                    con.execute(
                        "INSERT INTO staged_artifacts"
                        "(attempt_id,recipe_id,state,staging_path,inventory_json,"
                        " content_sha256) VALUES(?,?,?,?,?,?)",
                        (value.attempt_id, value.recipe.recipe_id, *expected),
                    )
                elif tuple(existing) != expected:
                    raise ArtifactConflictError(
                        "staged artifact identity changed during recovery")

                validation = by_recipe[value.recipe.recipe_id]
                report_json = strict_canonical_json(validation.report)
                expected_validation = (
                    validation.attempt_id, validation.recipe_id,
                    validation.validator_id, validation.content_sha256,
                    int(validation.passed), report_json,
                )
                existing_validation = con.execute(
                    "SELECT attempt_id,recipe_id,validator_id,content_sha256,"
                    " passed,report_json FROM validation_records "
                    "WHERE validation_id=?", (validation.validation_id,),
                ).fetchone()
                if existing_validation is None:
                    try:
                        con.execute(
                            "INSERT INTO validation_records"
                            "(validation_id,attempt_id,recipe_id,validator_id,"
                            " content_sha256,passed,report_json,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?)",
                            (validation.validation_id, *expected_validation,
                             time.time()),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ArtifactConflictError(
                            "validation identity conflicts with persisted record") from exc
                elif tuple(existing_validation) != expected_validation:
                    raise ArtifactConflictError(
                        "validation identity changed during recovery")

    def _authoritative_commit(
            self, spec: AttemptSpec,
            staged: tuple[StagedArtifact, ...],
            validations: tuple[ValidationRecord, ...],
            manifests: tuple[ArtifactManifest, ...]) -> CommitDisposition:
        validation_by_recipe = {
            value.recipe_id: value for value in validations}
        manifest_by_recipe = {value.recipe_id: value for value in manifests}
        artifact_set = [{
            "output_name": value.output_name,
            "recipe_id": value.recipe_id,
            "artifact_id": value.artifact_id,
            "content_sha256": value.content_sha256,
        } for value in sorted(manifests, key=lambda item: item.output_name)]
        artifact_set_sha256 = strict_hash(artifact_set)
        now = time.time()

        with self.store.transaction() as con:
            existing_commit = con.execute(
                "SELECT attempt_id,fencing_token,artifact_set_sha256 "
                "FROM task_commits WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
            if existing_commit is not None:
                same_winner = (
                    existing_commit[0] == spec.attempt_id
                    and int(existing_commit[1]) == spec.fencing_token)
                if same_winner and existing_commit[2] == artifact_set_sha256:
                    return CommitDisposition.DUPLICATE
                if same_winner:
                    return CommitDisposition.CONFLICT
                self._supersede_if_result_ready(con, spec)
                return CommitDisposition.STALE

            task = con.execute(
                "SELECT state,current_attempt_id,fence_counter,task_json "
                "FROM tasks WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
            attempt = con.execute(
                "SELECT state,run_id,task_id,fencing_token,spec_json "
                "FROM attempts WHERE attempt_id=?", (spec.attempt_id,),
            ).fetchone()
            lease = con.execute(
                "SELECT attempt_id,fencing_token FROM task_leases "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
            if task is None or attempt is None:
                raise RuntimeError("attempt/task disappeared before artifact commit")
            current_fence = (
                task[1] == spec.attempt_id
                and int(task[2]) == spec.fencing_token
                and attempt[1] == spec.run_id
                and attempt[2] == spec.task.task_id
                and int(attempt[3]) == spec.fencing_token
                and lease is not None
                and lease[0] == spec.attempt_id
                and int(lease[1]) == spec.fencing_token)
            if not current_fence:
                self._supersede_if_result_ready(con, spec)
                return CommitDisposition.STALE
            if TaskState(task[0]) is not TaskState.COMMITTING:
                raise RuntimeError(
                    f"authoritative commit requires COMMITTING, got {task[0]}")
            if AttemptState(attempt[0]) is not AttemptState.RESULT_READY:
                raise RuntimeError(
                    f"authoritative commit requires RESULT_READY, got {attempt[0]}")

            # Recheck exact recipe/validation bindings inside the same
            # transaction that makes them visible.
            for value in staged:
                recipe_row = con.execute(
                    "SELECT task_id,output_name,recipe_json FROM artifact_recipes "
                    "WHERE recipe_id=?", (value.recipe.recipe_id,),
                ).fetchone()
                if (recipe_row is None
                        or recipe_row[0] != spec.task.task_id
                        or recipe_row[1] != value.recipe.output_name):
                    raise ArtifactConflictError("artifact recipe binding changed")
                validation = validation_by_recipe[value.recipe.recipe_id]
                validation_row = con.execute(
                    "SELECT passed,content_sha256 FROM validation_records "
                    "WHERE validation_id=? AND attempt_id=? AND recipe_id=?",
                    (validation.validation_id, spec.attempt_id,
                     value.recipe.recipe_id),
                ).fetchone()
                if (validation_row is None or int(validation_row[0]) != 1
                        or validation_row[1] != value.content_sha256):
                    raise InvalidArtifactError(
                        "authoritative commit lacks a matching passed validation")

                manifest = manifest_by_recipe[value.recipe.recipe_id]
                manifest_json = strict_canonical_json(manifest.value)
                artifact_row = con.execute(
                    "SELECT recipe_id,content_sha256,manifest_path,manifest_json "
                    "FROM artifacts WHERE artifact_id=?",
                    (manifest.artifact_id,),
                ).fetchone()
                expected_artifact = (
                    manifest.recipe_id, manifest.content_sha256,
                    str(manifest.manifest_path), manifest_json,
                )
                if artifact_row is None:
                    con.execute(
                        "INSERT INTO artifacts"
                        "(artifact_id,recipe_id,content_sha256,manifest_path,"
                        " manifest_json,created_at) VALUES(?,?,?,?,?,?)",
                        (manifest.artifact_id, *expected_artifact, now),
                    )
                elif tuple(artifact_row) != expected_artifact:
                    raise ArtifactConflictError(
                        "artifact identity conflicts with persisted metadata")

            con.execute(
                "INSERT INTO task_commits"
                "(run_id,task_id,attempt_id,fencing_token,artifact_set_sha256,"
                " committed_at) VALUES(?,?,?,?,?,?)",
                (spec.run_id, spec.task.task_id, spec.attempt_id,
                 spec.fencing_token, artifact_set_sha256, now),
            )
            for manifest in manifests:
                validation = validation_by_recipe[manifest.recipe_id]
                con.execute(
                    "INSERT INTO artifact_commits"
                    "(run_id,recipe_id,task_id,output_name,artifact_id,"
                    " validation_id,attempt_id,fencing_token,committed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (spec.run_id, manifest.recipe_id, spec.task.task_id,
                     manifest.output_name, manifest.artifact_id,
                     validation.validation_id, spec.attempt_id,
                     spec.fencing_token, now),
                )
                con.execute(
                    "INSERT INTO task_output_slots"
                    "(run_id,task_id,output_name,artifact_id,recipe_id) "
                    "VALUES(?,?,?,?,?)",
                    (spec.run_id, spec.task.task_id, manifest.output_name,
                     manifest.artifact_id, manifest.recipe_id),
                )
                con.execute(
                    "INSERT INTO catalog_outbox"
                    "(run_id,recipe_id,artifact_id,state) VALUES(?,?,?,'PENDING')",
                    (spec.run_id, manifest.recipe_id, manifest.artifact_id),
                )
                recipe = next(
                    value.recipe for value in staged
                    if value.recipe.recipe_id == manifest.recipe_id)
                if recipe.scientific_binding is not None:
                    con.execute(
                        "INSERT INTO cube_projection_outbox"
                        "(run_id,recipe_id,artifact_id,state,created_at) "
                        "VALUES(?,?,?,'PENDING',?)",
                        (spec.run_id, manifest.recipe_id,
                         manifest.artifact_id, now),
                    )

            self.store._transition_attempt(
                con, spec.attempt_id, AttemptState.RESULT_READY,
                AttemptState.ACCEPTED, "ArtifactResultAccepted",
                {"artifact_set_sha256": artifact_set_sha256},
            )
            self.store._transition_task(
                con, spec.run_id, spec.task.task_id,
                TaskState.COMMITTING, TaskState.SUCCEEDED,
                "TaskArtifactsCommitted",
                {"artifact_set_sha256": artifact_set_sha256},
            )
            con.execute(
                "UPDATE tasks SET committed_at=?,error=NULL "
                "WHERE run_id=? AND task_id=?",
                (now, spec.run_id, spec.task.task_id),
            )
            con.execute("DELETE FROM task_leases WHERE attempt_id=?",
                        (spec.attempt_id,))
            self._unlock_dependents(con, spec, manifests, now)
            self.store._event(
                con,
                f"artifact-commit:{spec.run_id}:{spec.task.task_id}:"
                f"{artifact_set_sha256}",
                spec.run_id, "task", spec.task.task_id,
                TaskState.COMMITTING.value, TaskState.SUCCEEDED.value,
                "ArtifactCommitted", {
                    "attempt_id": spec.attempt_id,
                    "fencing_token": spec.fencing_token,
                    "artifact_set_sha256": artifact_set_sha256,
                    "artifacts": artifact_set,
                },
            )
            con.execute("UPDATE runs SET updated_at=? WHERE run_id=?",
                        (now, spec.run_id))
            self._hit(INSIDE_AUTHORITATIVE_COMMIT)
        return CommitDisposition.ACCEPTED

    def _unlock_dependents(self, con: sqlite3.Connection, spec: AttemptSpec,
                           manifests: tuple[ArtifactManifest, ...],
                           now: float) -> None:
        changed: dict[str, int] = {}
        for manifest in manifests:
            rows = con.execute(
                "SELECT downstream_task_id,input_name FROM task_dependencies "
                "WHERE run_id=? AND upstream_task_id=? AND upstream_output=? "
                "AND satisfied=0",
                (spec.run_id, spec.task.task_id, manifest.output_name),
            ).fetchall()
            con.execute(
                "UPDATE task_dependencies SET satisfied=1 WHERE run_id=? "
                "AND upstream_task_id=? AND upstream_output=? AND satisfied=0",
                (spec.run_id, spec.task.task_id, manifest.output_name),
            )
            for row in rows:
                downstream, input_name = row[0], row[1]
                changed[downstream] = changed.get(downstream, 0) + 1
                con.execute(
                    "UPDATE wake_conditions SET status='CONSUMED',consumed_at=? "
                    "WHERE run_id=? AND task_id=? AND kind='DEPENDENCY_COMMIT' "
                    "AND subject=? AND status='ACTIVE'",
                    (now, spec.run_id, downstream,
                     f"{spec.task.task_id}:{manifest.output_name}"),
                )
                self.store._event(
                    con,
                    f"dependency:{spec.run_id}:{downstream}:{input_name}:committed",
                    spec.run_id, "task", downstream, None, None,
                    "DependencyCommitted", {
                        "upstream_task_id": spec.task.task_id,
                        "upstream_output": manifest.output_name,
                        "input_name": input_name,
                        "artifact_id": manifest.artifact_id,
                    },
                )

        for downstream, count in sorted(changed.items()):
            task = con.execute(
                "SELECT state,unmet_dependencies FROM tasks "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, downstream),
            ).fetchone()
            if task is None:
                raise RuntimeError("downstream task disappeared during commit")
            if int(task[1]) < count:
                raise RuntimeError("dependency counter would become negative")
            con.execute(
                "UPDATE tasks SET unmet_dependencies=unmet_dependencies-? "
                "WHERE run_id=? AND task_id=?",
                (count, spec.run_id, downstream),
            )
            remaining = int(task[1]) - count
            if remaining == 0:
                if TaskState(task[0]) is not TaskState.WAITING:
                    raise RuntimeError(
                        "all dependencies committed for a non-WAITING task")
                self.store._transition_task(
                    con, spec.run_id, downstream,
                    TaskState.WAITING, TaskState.READY,
                    "AllDependenciesCommitted", {},
                )
                con.execute(
                    "UPDATE tasks SET ready_at=? WHERE run_id=? AND task_id=?",
                    (now, spec.run_id, downstream),
                )

    # ------------------------------------------------------------------
    # Fencing, invalid output, and utility helpers
    # ------------------------------------------------------------------
    def _preflight(self, spec: AttemptSpec) -> CommitDisposition | None:
        with self.store.connect() as con:
            attempt = con.execute(
                "SELECT run_id,task_id,fencing_token,state,spec_json "
                "FROM attempts WHERE attempt_id=?", (spec.attempt_id,),
            ).fetchone()
            task = con.execute(
                "SELECT current_attempt_id,fence_counter,state FROM tasks "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
            commit = con.execute(
                "SELECT attempt_id,fencing_token FROM task_commits "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
        if attempt is None or task is None:
            raise KeyError(spec.attempt_id)
        if (attempt[0] != spec.run_id
                or attempt[1] != spec.task.task_id
                or int(attempt[2]) != spec.fencing_token):
            raise ArtifactConflictError("attempt identity conflicts with control store")
        if commit is not None:
            if (commit[0] == spec.attempt_id
                    and int(commit[1]) == spec.fencing_token):
                return None  # compute the candidate set; duplicate requires same bytes
            self._mark_stale_attempt(spec)
            return CommitDisposition.STALE
        if (task[0] != spec.attempt_id
                or int(task[1]) != spec.fencing_token
                or AttemptState(attempt[3]) is not AttemptState.RESULT_READY
                or TaskState(task[2]) not in {
                    TaskState.VALIDATING, TaskState.COMMITTING}):
            self._mark_stale_attempt(spec)
            return CommitDisposition.STALE
        return None

    def _task_state(self, spec: AttemptSpec) -> TaskState:
        with self.store.connect() as con:
            row = con.execute(
                "SELECT state FROM tasks WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
        if row is None:
            raise KeyError((spec.run_id, spec.task.task_id))
        return TaskState(row[0])

    def _mark_stale_attempt(self, spec: AttemptSpec) -> None:
        """Persist a late RESULT_READY disposition when that is still legal."""
        with self.store.transaction() as con:
            self._supersede_if_result_ready(con, spec)

    def _supersede_if_result_ready(self, con: sqlite3.Connection,
                                   spec: AttemptSpec) -> None:
        row = con.execute(
            "SELECT state FROM attempts WHERE attempt_id=?",
            (spec.attempt_id,),
        ).fetchone()
        if row is not None and AttemptState(row[0]) is AttemptState.RESULT_READY:
            self.store._transition_attempt(
                con, spec.attempt_id, AttemptState.RESULT_READY,
                AttemptState.SUPERSEDED, "StaleArtifactResultRejected",
                {"fencing_token": spec.fencing_token},
            )

    def _mark_invalid(self, spec: AttemptSpec, error: str) -> None:
        message = error[:4096]
        with self.store.connect() as con:
            row = con.execute(
                "SELECT state,current_attempt_id,fence_counter FROM tasks "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
        if row is None or row[1] != spec.attempt_id or int(row[2]) != spec.fencing_token:
            return
        state = TaskState(row[0])
        if state is TaskState.VALIDATING:
            self.store.mark_invalid_output(
                spec.run_id, spec.task.task_id, spec.attempt_id, message)
            return
        if state is not TaskState.COMMITTING:
            return
        with self.store.transaction() as con:
            task = con.execute(
                "SELECT state,current_attempt_id,fence_counter FROM tasks "
                "WHERE run_id=? AND task_id=?",
                (spec.run_id, spec.task.task_id),
            ).fetchone()
            if (task is None or TaskState(task[0]) is not TaskState.COMMITTING
                    or task[1] != spec.attempt_id
                    or int(task[2]) != spec.fencing_token):
                return
            self.store._transition_task(
                con, spec.run_id, spec.task.task_id,
                TaskState.COMMITTING, TaskState.FAILED,
                "ArtifactCommitRecoveryFailed", {"error": message},
            )
            attempt = con.execute(
                "SELECT state FROM attempts WHERE attempt_id=?",
                (spec.attempt_id,),
            ).fetchone()
            if attempt is not None and AttemptState(attempt[0]) is AttemptState.RESULT_READY:
                self.store._transition_attempt(
                    con, spec.attempt_id, AttemptState.RESULT_READY,
                    AttemptState.FAILED, "OutputRejected", {"error": message},
                )
            con.execute(
                "UPDATE tasks SET error=? WHERE run_id=? AND task_id=?",
                (message, spec.run_id, spec.task.task_id),
            )
            con.execute("DELETE FROM task_leases WHERE attempt_id=?",
                        (spec.attempt_id,))

    def _require_beneath(self, raw: Path, label: str, *,
                         may_not_exist: bool = False) -> Path:
        path = raw if raw.is_absolute() else self.runtime_root / raw
        lexical = Path(os.path.abspath(os.fspath(path)))
        try:
            lexical.relative_to(self.runtime_root)
        except ValueError as exc:
            raise ValueError(f"{label} escapes the runtime root") from exc
        if not may_not_exist and not lexical.exists():
            raise InvalidArtifactError(f"{label} does not exist")
        return lexical

    @staticmethod
    def _reject_symlink_components(path: Path, root: Path) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise InvalidArtifactError("path escapes its confinement root") from exc
        current = root
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError as exc:
                raise InvalidArtifactError(f"path component is missing: {current}") from exc
            if stat.S_ISLNK(mode):
                raise InvalidArtifactError(
                    f"symbolic links are forbidden in staged output: {current}")

    def _hit(self, name: str) -> None:
        if self.failpoint is not None:
            self.failpoint(name)


def _no_follow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _open_regular_readonly(path: Path) -> int:
    try:
        fd = os.open(path, os.O_RDONLY | _no_follow_flag())
    except OSError as exc:
        raise InvalidArtifactError(f"cannot safely open staged file: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InvalidArtifactError(f"staged output is not a regular file: {path}")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while finalizing immutable artifact")
        view = view[written:]


def _read_verified_bytes(path: Path, expected_sha256: str,
                         expected_size: int) -> bytes:
    fd = _open_regular_readonly(path)
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    size = 0
    try:
        while True:
            block = os.read(fd, _READ_CHUNK)
            if not block:
                break
            chunks.append(block)
            digest.update(block)
            size += len(block)
    finally:
        os.close(fd)
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise InvalidArtifactError(f"immutable object checksum mismatch: {path}")
    return b"".join(chunks)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _decode_strict_json(payload: bytes) -> Any:
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise InvalidArtifactError("JSON artifact is not UTF-8") from exc

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidArtifactError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise InvalidArtifactError(f"non-finite JSON constant {value!r}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise InvalidArtifactError("artifact is not valid JSON") from exc
    _assert_finite_json(value)
    # This is also the no-coercion/strict-identity check.
    strict_canonical_json(value)
    return value


def _read_strict_json(path: Path, *, max_bytes: int) -> Any:
    info = path.stat(follow_symlinks=False)
    if info.st_size > max_bytes:
        raise InvalidArtifactError(f"JSON control manifest is too large: {path}")
    fd = _open_regular_readonly(path)
    try:
        payload = b""
        while True:
            block = os.read(fd, min(_READ_CHUNK, max_bytes + 1 - len(payload)))
            if not block:
                break
            payload += block
            if len(payload) > max_bytes:
                raise InvalidArtifactError(
                    f"JSON control manifest is too large: {path}")
    finally:
        os.close(fd)
    return _decode_strict_json(payload)


def _assert_finite_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidArtifactError("JSON contains a non-finite number")
    if isinstance(value, list):
        for item in value:
            _assert_finite_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            _assert_finite_json(item)


def _validate_field_configuration(configuration: dict[str, Any]) -> None:
    expected = {
        "kind", "descriptor_id", "crs", "grid_affine_convention",
        "grid_axis_order", "grid_shape", "grid_affine",
        "grid_support_bounds", "temporal", "component_names",
    }
    if set(configuration) != expected:
        raise InvalidArtifactError(
            "field_json_v2 validator configuration has invalid fields")
    descriptor_id = configuration["descriptor_id"]
    if (not isinstance(descriptor_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", descriptor_id) is None):
        raise InvalidArtifactError(
            "field_json_v2 requires a descriptor SHA-256 identity")
    if not isinstance(configuration["crs"], str) or not configuration["crs"]:
        raise InvalidArtifactError("field_json_v2 requires a CRS")
    if configuration["grid_affine_convention"] != GRID_AFFINE_CONVENTION:
        raise InvalidArtifactError(
            "field_json_v2 has an unsupported grid affine convention")
    axis_order = configuration["grid_axis_order"]
    if (not isinstance(axis_order, (list, tuple)) or len(axis_order) != 2
            or any(not isinstance(value, str) or not value
                   for value in axis_order)
            or axis_order[0] == axis_order[1]):
        raise InvalidArtifactError(
            "field_json_v2 grid_axis_order must name distinct x/y axes")
    shape = configuration["grid_shape"]
    if (not isinstance(shape, (list, tuple)) or len(shape) != 2
            or any(type(value) is not int or value <= 0 for value in shape)):
        raise InvalidArtifactError(
            "field_json_v2 grid_shape must be [positive_y, positive_x]")
    affine = configuration["grid_affine"]
    if not isinstance(affine, (list, tuple)) or len(affine) != 6:
        raise InvalidArtifactError(
            "field_json_v2 grid_affine must contain six decimal values")
    try:
        affine_values = tuple(Decimal(value) for value in affine)
    except (InvalidOperation, TypeError) as exc:
        raise InvalidArtifactError(
            "field_json_v2 grid_affine values must be decimal") from exc
    if (any(not value.is_finite() for value in affine_values)
            or affine_values[1] != 0 or affine_values[3] != 0
            or affine_values[0] == 0 or affine_values[4] == 0):
        raise InvalidArtifactError(
            "field_json_v2 requires finite nonzero signed axis-aligned steps")
    raw_bounds = configuration["grid_support_bounds"]
    if not isinstance(raw_bounds, (list, tuple)) or len(raw_bounds) != 4:
        raise InvalidArtifactError(
            "field_json_v2 grid_support_bounds must contain four decimals")
    try:
        bounds = tuple(Decimal(value) for value in raw_bounds)
    except (InvalidOperation, TypeError) as exc:
        raise InvalidArtifactError(
            "field_json_v2 grid_support_bounds must be decimal") from exc
    if (any(not value.is_finite() for value in bounds)
            or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
        raise InvalidArtifactError(
            "field_json_v2 grid_support_bounds must be finite ordered bounds")
    rows, columns = shape
    a, _, c, _, e, f = affine_values
    x_last = c + a * (columns - 1)
    y_last = f + e * (rows - 1)
    x_edges = (c - a / 2, x_last + a / 2)
    y_edges = (f - e / 2, y_last + e / 2)
    expected_bounds = (
        min(x_edges), min(y_edges), max(x_edges), max(y_edges))
    if bounds != expected_bounds:
        raise InvalidArtifactError(
            "field_json_v2 support bounds do not match sample-centre grid edges")
    names = configuration["component_names"]
    if (not isinstance(names, (list, tuple)) or not names
            or any(not isinstance(value, str) or not value for value in names)
            or len(names) != len(set(names))):
        raise InvalidArtifactError(
            "field_json_v2 component_names must be unique strings")
    temporal = configuration["temporal"]
    if not isinstance(temporal, dict) or set(temporal) != {
            "kind", "start", "end", "cadence_s"}:
        raise InvalidArtifactError(
            "field_json_v2 temporal validator configuration is invalid")
    if temporal["kind"] == "TIME_INVARIANT":
        if any(temporal[value] is not None
               for value in ("start", "end", "cadence_s")):
            raise InvalidArtifactError(
                "time-invariant field validation cannot declare a timeline")
    elif temporal["kind"] == "SERIES":
        if any(not isinstance(temporal[value], str) or not temporal[value]
               for value in ("start", "end", "cadence_s")):
            raise InvalidArtifactError(
                "series field validation requires start/end/cadence")
        start = _parse_utc(temporal["start"])
        end = _parse_utc(temporal["end"])
        cadence = _positive_decimal(temporal["cadence_s"], "cadence")
        if start >= end or _expected_series_count(start, end, cadence) <= 0:
            raise InvalidArtifactError(
                "series field validation has an invalid temporal lattice")
    else:
        raise InvalidArtifactError(
            "field_json_v2 temporal kind must be TIME_INVARIANT or SERIES")


def _validate_field_json_v2(
        value: Any, configuration: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
            "schema", "crs", "axis_order", "x", "y", "time", "components"}:
        raise InvalidArtifactError(
            "field_json_v2 payload has an invalid top-level schema")
    if value["schema"] != FIELD_JSON_SCHEMA:
        raise InvalidArtifactError("field_json_v2 payload schema is invalid")
    if value["crs"] != configuration["crs"]:
        raise InvalidArtifactError("field_json_v2 payload CRS does not match")
    if (not isinstance(value["axis_order"], list)
            or value["axis_order"] != list(configuration["grid_axis_order"])):
        raise InvalidArtifactError(
            "field_json_v2 payload axis names/order do not match descriptor")
    x = _signed_axis_numbers(value["x"], "x")
    y = _signed_axis_numbers(value["y"], "y")
    expected_y, expected_x = configuration["grid_shape"]
    if len(x) != expected_x or len(y) != expected_y:
        raise InvalidArtifactError(
            "field_json_v2 coordinate lengths do not match descriptor grid")
    affine = tuple(Decimal(item) for item in configuration["grid_affine"])
    expected_x_values = [affine[2] + affine[0] * index
                         for index in range(expected_x)]
    expected_y_values = [affine[5] + affine[4] * index
                         for index in range(expected_y)]
    if ([Decimal(str(item)) for item in x] != expected_x_values
            or [Decimal(str(item)) for item in y] != expected_y_values):
        raise InvalidArtifactError(
            "field_json_v2 coordinates do not match descriptor affine")
    times = value["time"]
    if (not isinstance(times, list) or not times
            or any(not isinstance(item, str) or not item for item in times)
            or len(times) != len(set(times))):
        raise InvalidArtifactError(
            "field_json_v2 time must be a non-empty unique string array")
    temporal = configuration["temporal"]
    if temporal["kind"] == "TIME_INVARIANT":
        expected_times = ["TIME_INVARIANT"]
    else:
        start = _parse_utc(temporal["start"])
        end = _parse_utc(temporal["end"])
        cadence = _positive_decimal(temporal["cadence_s"], "cadence")
        expected_times = _series_lattice(start, end, cadence)
    if times != expected_times:
        raise InvalidArtifactError(
            "field_json_v2 time coordinates do not match descriptor lattice")

    components = value["components"]
    if not isinstance(components, dict) or not components:
        raise InvalidArtifactError(
            "field_json_v2 components must be a non-empty object")
    if any(not isinstance(name, str) or not name for name in components):
        raise InvalidArtifactError("field_json_v2 component name is invalid")
    expected_names = configuration["component_names"]
    if sorted(components) != sorted(expected_names):
        raise InvalidArtifactError(
            "field_json_v2 components do not match the output contract")
    for name, tensor in components.items():
        if not isinstance(tensor, list) or len(tensor) != len(times):
            raise InvalidArtifactError(
                f"field_json_v2 component {name!r} has the wrong time shape")
        for plane in tensor:
            if not isinstance(plane, list) or len(plane) != len(y):
                raise InvalidArtifactError(
                    f"field_json_v2 component {name!r} has the wrong y shape")
            for row in plane:
                if not isinstance(row, list) or len(row) != len(x):
                    raise InvalidArtifactError(
                        f"field_json_v2 component {name!r} has the wrong x shape")
                for item in row:
                    if (isinstance(item, bool)
                            or not isinstance(item, (int, float))
                            or not math.isfinite(float(item))):
                        raise InvalidArtifactError(
                            f"field_json_v2 component {name!r} is not finite numeric")


def _signed_axis_numbers(value: Any, label: str) -> list[float]:
    if (not isinstance(value, list) or not value
            or any(isinstance(item, bool)
                   or not isinstance(item, (int, float))
                   or not math.isfinite(float(item)) for item in value)):
        raise InvalidArtifactError(
            f"field_json_v2 {label} must be finite numeric coordinates")
    result = [float(item) for item in value]
    differences = [right - left for left, right in zip(result, result[1:])]
    if differences and (any(value == 0 for value in differences)
                        or min(differences) < 0 < max(differences)):
        raise InvalidArtifactError(
            f"field_json_v2 {label} coordinates must be strictly monotonic")
    return result


def _positive_decimal(value: str, label: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise InvalidArtifactError(
            f"field_json_v2 {label} must be decimal") from exc
    if not result.is_finite() or result <= 0:
        raise InvalidArtifactError(
            f"field_json_v2 {label} must be positive and finite")
    return result


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise InvalidArtifactError(
            "field_json_v2 time is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidArtifactError("field_json_v2 time lacks a UTC offset")
    return parsed.astimezone(timezone.utc)


def _expected_series_count(
        start: datetime, end: datetime, cadence: Decimal) -> int:
    duration_us = int((end - start).total_seconds() * 1_000_000)
    cadence_us_decimal = cadence * Decimal(1_000_000)
    if cadence_us_decimal != cadence_us_decimal.to_integral_value():
        raise InvalidArtifactError(
            "field_json_v2 cadence must resolve to integral microseconds")
    cadence_us = int(cadence_us_decimal)
    if duration_us % cadence_us:
        raise InvalidArtifactError(
            "field_json_v2 end must lie on the cadence lattice")
    return duration_us // cadence_us


def _series_lattice(
        start: datetime, end: datetime, cadence: Decimal) -> list[str]:
    count = _expected_series_count(start, end, cadence)
    cadence_us = int(cadence * Decimal(1_000_000))
    result: list[str] = []
    for index in range(count):
        value = start + timedelta(microseconds=cadence_us * index)
        text = value.isoformat(timespec="microseconds")
        result.append(text.removesuffix("+00:00").replace(".000000", "") + "Z")
    return result


__all__ = [
    "AFTER_AUTHORITATIVE_COMMIT",
    "AFTER_BEGIN_COMMITTING",
    "AFTER_MANIFESTS_FINALIZED",
    "AFTER_OBJECTS_FINALIZED",
    "AFTER_RESULT_PARSED",
    "AFTER_VALIDATION_RECORDED",
    "ArtifactCommitter",
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactManifest",
    "BEFORE_AUTHORITATIVE_COMMIT",
    "CommitDisposition",
    "FAILPOINTS",
    "INSIDE_AUTHORITATIVE_COMMIT",
    "InvalidArtifactError",
    "StagedArtifact",
    "ValidationRecord",
]
