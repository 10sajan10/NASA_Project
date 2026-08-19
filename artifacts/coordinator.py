"""Durable target requests and crash-replayable native-output events."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from contracts import RequirementUse
from cube.entries import DatasetRef
from engine.runtime.identity import (
    require_object_fields,
    strict_canonical_json,
    strict_hash,
)
from resolution import SelectionConstraints

from .manifest import WorkflowManifest
from .records import ArtifactInput, ArtifactRecord
from .service import ArtifactWorkflowResolver


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS target_requests (
    target_id       TEXT PRIMARY KEY,
    request_json    TEXT NOT NULL,
    status          TEXT NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 0,
    workflow_manifest_json TEXT,
    artifact_snapshot_id TEXT,
    submitted_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS target_requests_status ON target_requests(status);
CREATE TABLE IF NOT EXISTS artifact_output_events (
    event_id         TEXT PRIMARY KEY,
    event_json       TEXT NOT NULL,
    status           TEXT NOT NULL,
    registered_record_ids_json TEXT,
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS artifact_output_events_status
    ON artifact_output_events(status, event_id);
"""


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")


class TargetStatus(str, Enum):
    PLANNED_WORKFLOW = "PLANNED_WORKFLOW"
    SATISFIED_BY_ARTIFACT = "SATISFIED_BY_ARTIFACT"
    INCOMPLETE = "INCOMPLETE"
    UNSATISFIABLE = "UNSATISFIABLE"
    ERROR = "ERROR"


class OutputEventStatus(str, Enum):
    PENDING = "PENDING"
    REGISTERED = "REGISTERED"
    APPLIED = "APPLIED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class TargetRequest:
    target_id: str
    roots: tuple[RequirementUse, ...]
    constraints: SelectionConstraints
    require_proven_optimal: bool = True

    def __post_init__(self) -> None:
        _digest(self.target_id, "target request ID")
        if (not isinstance(self.roots, tuple) or not self.roots
                or not all(isinstance(value, RequirementUse)
                           for value in self.roots)
                or self.roots != tuple(sorted(
                    self.roots, key=lambda value: value.requirement_use_id))
                or len({value.requirement_use_id for value in self.roots})
                != len(self.roots)):
            raise ValueError("target roots must be unique typed uses in order")
        if not isinstance(self.constraints, SelectionConstraints):
            raise TypeError("target constraints must be SelectionConstraints")
        if type(self.require_proven_optimal) is not bool:
            raise TypeError("target optimality policy must be bool")
        if self.target_id != self.expected_id():
            raise ValueError("target request identity does not verify")

    @classmethod
    def bind(
        cls,
        roots: Iterable[RequirementUse],
        *,
        constraints: SelectionConstraints | None = None,
        require_proven_optimal: bool = True,
    ) -> "TargetRequest":
        values = tuple(sorted(
            roots, key=lambda value: value.requirement_use_id))
        policy = constraints or SelectionConstraints.bind()
        provisional = object.__new__(cls)
        object.__setattr__(provisional, "target_id", "")
        object.__setattr__(provisional, "roots", values)
        object.__setattr__(provisional, "constraints", policy)
        object.__setattr__(provisional, "require_proven_optimal",
                           require_proven_optimal)
        return cls(strict_hash(provisional._payload()), values, policy,
                   require_proven_optimal)

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "stage10b-target-request-v1",
            "roots": [value.to_dict() for value in self.roots],
            "constraints": self.constraints.to_dict(),
            "require_proven_optimal": self.require_proven_optimal,
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload())

    def to_dict(self) -> dict[str, Any]:
        return {"target_id": self.target_id, **self._payload()}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TargetRequest":
        raw = require_object_fields(value, {
            "schema", "target_id", "roots", "constraints",
            "require_proven_optimal",
        }, "TargetRequest")
        if raw.pop("schema") != "stage10b-target-request-v1":
            raise ValueError("unsupported target request schema")
        if not isinstance(raw["roots"], list):
            raise TypeError("TargetRequest.roots must be an array")
        raw["roots"] = tuple(
            RequirementUse.from_dict(item) for item in raw["roots"])
        raw["constraints"] = SelectionConstraints.from_dict(raw["constraints"])
        return cls(**raw)


@dataclass(frozen=True)
class ArtifactOutputEvent:
    event_id: str
    producer_id: str
    records: tuple[ArtifactRecord, ...]

    def __post_init__(self) -> None:
        _digest(self.event_id, "artifact output event ID")
        if not isinstance(self.producer_id, str) or not self.producer_id:
            raise ValueError("output event producer_id must be text")
        if (not isinstance(self.records, tuple) or not self.records
                or not all(isinstance(value, ArtifactRecord)
                           for value in self.records)
                or self.records != tuple(sorted(
                    self.records, key=lambda value: value.output_port_id))
                or len({value.output_port_id for value in self.records})
                != len(self.records)):
            raise ValueError("output event records must be unique by port")
        if any(value.producer_id != self.producer_id for value in self.records):
            raise ValueError("output event records name another producer")
        if self.event_id != self.expected_id():
            raise ValueError("output event identity does not verify")

    @classmethod
    def bind(cls, producer_id: str, records: Iterable[ArtifactRecord]
             ) -> "ArtifactOutputEvent":
        values = tuple(sorted(records, key=lambda value: value.output_port_id))
        payload = {
            "schema": "stage10b-artifact-output-event-v1",
            "producer_id": producer_id,
            "records": [value.to_dict() for value in values],
        }
        return cls(strict_hash(payload), producer_id, values)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage10b-artifact-output-event-v1",
            "producer_id": self.producer_id,
            "records": [value.to_dict() for value in self.records],
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage10b-artifact-output-event-v1",
            "event_id": self.event_id,
            "producer_id": self.producer_id,
            "records": [value.to_dict() for value in self.records],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ArtifactOutputEvent":
        raw = require_object_fields(value, {
            "schema", "event_id", "producer_id", "records",
        }, "ArtifactOutputEvent")
        if raw.pop("schema") != "stage10b-artifact-output-event-v1":
            raise ValueError("unsupported artifact output event schema")
        if not isinstance(raw["records"], list):
            raise TypeError("ArtifactOutputEvent.records must be an array")
        raw["records"] = tuple(
            ArtifactRecord.from_dict(item) for item in raw["records"])
        return cls(**raw)


@dataclass(frozen=True)
class TargetState:
    request: TargetRequest
    status: TargetStatus
    revision: int
    workflow_manifest: WorkflowManifest | None


class ArtifactTargetCoordinator:
    """Convergent local coordinator for targets and artifact output events.

    Registry publication and this coordination database are separate SQLite
    transactions.  The state machine is deliberately replayable: PENDING may
    be registered repeatedly, and REGISTERED may re-resolve targets repeatedly,
    before APPLIED closes the event.
    """

    def __init__(self, path: str | Path, resolver: ArtifactWorkflowResolver):
        if not isinstance(resolver, ArtifactWorkflowResolver):
            raise TypeError("coordinator requires ArtifactWorkflowResolver")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.resolver = resolver
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def submit_target(
        self,
        roots: Iterable[RequirementUse],
        *,
        constraints: SelectionConstraints | None = None,
        require_proven_optimal: bool = True,
    ) -> TargetState:
        request = TargetRequest.bind(
            roots, constraints=constraints,
            require_proven_optimal=require_proven_optimal)
        encoded = strict_canonical_json(request.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_json FROM target_requests WHERE target_id=?",
                (request.target_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO target_requests "
                    "(target_id,request_json,status) VALUES (?,?,?)",
                    (request.target_id, encoded, TargetStatus.ERROR.value),
                )
            elif row["request_json"] != encoded:
                raise ValueError("target identity has conflicting request bytes")
            connection.commit()
        return self.resolve_target(request.target_id)

    @staticmethod
    def _status_for(outcome) -> TargetStatus:
        plan = outcome.resolution.selection.plan
        if outcome.resolution.eligible_for_binding and plan is not None:
            if not plan.selected_invocation_ids and outcome.selected_artifacts:
                return TargetStatus.SATISFIED_BY_ARTIFACT
            return TargetStatus.PLANNED_WORKFLOW
        name = outcome.resolution.status.value
        if name == "UNSATISFIABLE":
            return TargetStatus.UNSATISFIABLE
        if name in ("INCOMPLETE", "FEASIBLE_NOT_PROVEN_OPTIMAL"):
            return TargetStatus.INCOMPLETE
        return TargetStatus.ERROR

    def resolve_target(self, target_id: str) -> TargetState:
        request = self._request(target_id)
        outcome = self.resolver.resolve(
            request.roots,
            constraints=request.constraints,
            require_proven_optimal=request.require_proven_optimal,
        )
        manifest = WorkflowManifest.bind(target_id, request.roots, outcome)
        status = self._status_for(outcome)
        encoded = strict_canonical_json(manifest.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT workflow_manifest_json FROM target_requests "
                "WHERE target_id=?", (target_id,)).fetchone()
            if current is None:
                raise KeyError(target_id)
            changed = current["workflow_manifest_json"] != encoded
            connection.execute(
                "UPDATE target_requests SET status=?, "
                "revision=revision+?, workflow_manifest_json=?, "
                "artifact_snapshot_id=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE target_id=?",
                (status.value, int(changed), encoded,
                 manifest.artifact_snapshot_id, target_id),
            )
            connection.commit()
        return self.target(target_id)

    def _request(self, target_id: str) -> TargetRequest:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_json FROM target_requests WHERE target_id=?",
                (target_id,),
            ).fetchone()
        if row is None:
            raise KeyError(target_id)
        return TargetRequest.from_dict(json.loads(row["request_json"]))

    def target(self, target_id: str) -> TargetState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM target_requests WHERE target_id=?",
                (target_id,),
            ).fetchone()
        if row is None:
            raise KeyError(target_id)
        manifest = (None if row["workflow_manifest_json"] is None else
                    WorkflowManifest.from_dict(
                        json.loads(row["workflow_manifest_json"])))
        return TargetState(
            TargetRequest.from_dict(json.loads(row["request_json"])),
            TargetStatus(row["status"]), row["revision"], manifest)

    def targets(self) -> tuple[TargetState, ...]:
        with self._connect() as connection:
            ids = tuple(row["target_id"] for row in connection.execute(
                "SELECT target_id FROM target_requests ORDER BY target_id"))
        return tuple(self.target(value) for value in ids)

    def enqueue_output_event(
        self,
        *,
        producer_id: str,
        outputs: dict[str, DatasetRef],
        inputs: Iterable[ArtifactInput] = (),
    ) -> ArtifactOutputEvent:
        if not isinstance(outputs, dict) or not outputs:
            raise ValueError("output event requires a non-empty mapping")
        input_values = tuple(inputs)
        prepared: list[ArtifactRecord] = []
        for port_id in sorted(outputs):
            output = outputs[port_id]
            if not isinstance(output, DatasetRef):
                raise TypeError("output event values must be DatasetRef")
            if output.output_port_id != port_id:
                raise ValueError("output event port and DatasetRef disagree")
            if output.descriptor is None:
                raise ValueError("output event requires a full descriptor")
            prepared.append(self.resolver.artifact_registry.prepare_file(
                output.path, output.descriptor,
                media_type=output.media_type,
                producer_id=producer_id,
                producer_version=output.producer_version,
                output_port_id=port_id,
                inputs=input_values,
                evidence_profile_id=output.evidence_profile_id,
                metadata=output.detail,
            ))
        return self.enqueue_records(producer_id, prepared)

    def enqueue_records(
        self, producer_id: str, records: Iterable[ArtifactRecord],
    ) -> ArtifactOutputEvent:
        """Durably enqueue already verified records from an authority bridge."""
        event = ArtifactOutputEvent.bind(producer_id, records)
        encoded = strict_canonical_json(event.to_dict())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_json FROM artifact_output_events WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO artifact_output_events "
                    "(event_id,event_json,status) VALUES (?,?,?)",
                    (event.event_id, encoded, OutputEventStatus.PENDING.value),
                )
            elif row["event_json"] != encoded:
                raise ValueError("output event identity has conflicting bytes")
            connection.commit()
        return event

    def event_status(self, event_id: str) -> OutputEventStatus:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM artifact_output_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return OutputEventStatus(row["status"])

    def _event(self, event_id: str) -> ArtifactOutputEvent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_json FROM artifact_output_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return ArtifactOutputEvent.from_dict(json.loads(row["event_json"]))

    def process_event(self, event_id: str) -> OutputEventStatus:
        status = self.event_status(event_id)
        if status is OutputEventStatus.APPLIED:
            return status
        event = self._event(event_id)
        if status is OutputEventStatus.PENDING:
            try:
                records = self.resolver.artifact_registry.register_records(
                    event.records)
            except Exception as exc:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE artifact_output_events SET status=?,error=?,"
                        "updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                        (OutputEventStatus.FAILED.value,
                         f"{type(exc).__name__}:{exc}", event_id),
                    )
                raise
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE artifact_output_events SET status=?,"
                    "registered_record_ids_json=?,error='',"
                    "updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                    (OutputEventStatus.REGISTERED.value,
                     strict_canonical_json(
                         [value.record_id for value in records]), event_id),
                )
                connection.commit()
            status = OutputEventStatus.REGISTERED
        if status is OutputEventStatus.REGISTERED:
            for target in self.targets():
                self.resolve_target(target.request.target_id)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE artifact_output_events SET status=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                    (OutputEventStatus.APPLIED.value, event_id),
                )
            status = OutputEventStatus.APPLIED
        return status

    def recover(self) -> tuple[str, ...]:
        with self._connect() as connection:
            ids = tuple(row["event_id"] for row in connection.execute(
                "SELECT event_id FROM artifact_output_events "
                "WHERE status IN (?,?) ORDER BY event_id",
                (OutputEventStatus.PENDING.value,
                 OutputEventStatus.REGISTERED.value),
            ))
        for event_id in ids:
            self.process_event(event_id)
        return ids

    def output_arrived(self, *, producer_id: str,
                       outputs: dict[str, DatasetRef],
                       inputs: Iterable[ArtifactInput] = ()) -> ArtifactOutputEvent:
        event = self.enqueue_output_event(
            producer_id=producer_id, outputs=outputs, inputs=inputs)
        self.process_event(event.event_id)
        return event


__all__ = [
    "ArtifactOutputEvent",
    "ArtifactTargetCoordinator",
    "OutputEventStatus",
    "TargetRequest",
    "TargetState",
    "TargetStatus",
]
