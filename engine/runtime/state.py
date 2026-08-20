"""Single-writer SQLite state and event journal for the Stage-1 controller."""
from __future__ import annotations

import contextlib
import fcntl
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from .identity import strict_canonical_json, strict_hash, strict_json_loads
from .native import verify_native_file
from .types import (
    ArtifactRecipe,
    AttemptInputReceipt,
    AttemptSpec,
    AttemptState,
    BoundExecutionGraph,
    BoundTask,
    ExternalHandle,
    ExternalArtifactInputBinding,
    InputArtifactSource,
    ProviderObservation,
    RegisteredArtifactDelivery,
    RegisteredArtifactInputBinding,
    RegisteredArtifactInputReceipt,
    RunState,
    TaskState,
    TaskInputLineage,
    WakeKind,
    attempt_id as make_attempt_id,
)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS controller_sessions (
  epoch INTEGER PRIMARY KEY AUTOINCREMENT,
  controller_id TEXT NOT NULL UNIQUE,
  started_at REAL NOT NULL,
  stopped_at REAL,
  heartbeat_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS bound_graphs (
  plan_id TEXT PRIMARY KEY,
  graph_json TEXT NOT NULL,
  graph_sha256 TEXT NOT NULL,
  created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES bound_graphs(plan_id),
  state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  state_version INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  error TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS tasks (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  task_id TEXT NOT NULL,
  task_key TEXT NOT NULL,
  task_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'WAITING','BLOCKED','READY','RUNNING','VALIDATING','COMMITTING',
    'RETRY_WAIT','SUCCEEDED','FAILED','CANCELLED','INVALID_OUTPUT','LOST')),
  state_version INTEGER NOT NULL DEFAULT 0,
  unmet_dependencies INTEGER NOT NULL CHECK(unmet_dependencies >= 0),
  fence_counter INTEGER NOT NULL DEFAULT 0 CHECK(fence_counter >= 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  current_attempt_id TEXT,
  ready_at REAL,
  error TEXT,
  committed_at REAL,
  PRIMARY KEY(run_id, task_id),
  UNIQUE(run_id, task_key)
) STRICT;
CREATE INDEX IF NOT EXISTS tasks_ready_idx
  ON tasks(run_id, state, ready_at, task_id);
CREATE TABLE IF NOT EXISTS task_dependencies (
  run_id TEXT NOT NULL,
  upstream_task_id TEXT NOT NULL,
  upstream_output TEXT NOT NULL,
  downstream_task_id TEXT NOT NULL,
  input_name TEXT NOT NULL,
  satisfied INTEGER NOT NULL DEFAULT 0 CHECK(satisfied IN (0,1)),
  PRIMARY KEY(run_id, downstream_task_id, input_name),
  FOREIGN KEY(run_id, upstream_task_id) REFERENCES tasks(run_id, task_id),
  FOREIGN KEY(run_id, downstream_task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS task_external_inputs (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  input_name TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  source_run_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  manifest_path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id, task_id, input_name),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS task_registered_inputs (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  input_name TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  record_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  record_json TEXT NOT NULL,
  delivery TEXT NOT NULL CHECK(delivery IN ('JSON_VALUE','NATIVE_FILE_POINTER')),
  PRIMARY KEY(run_id, task_id, input_name),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  fencing_token INTEGER NOT NULL,
  attempt_token TEXT NOT NULL UNIQUE,
  deployment_id TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  provider TEXT NOT NULL,
  stage_dir TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'CREATED','SUBMITTING','SUBMISSION_UNKNOWN','SUBMITTED','RUNNING',
    'RESULT_READY','ACCEPTED','SUPERSEDED','FAILED','LOST','CANCELLED')),
  state_version INTEGER NOT NULL DEFAULT 0,
  result_manifest_path TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id),
  UNIQUE(run_id, task_id, attempt_number),
  UNIQUE(run_id, task_id, fencing_token)
) STRICT;
CREATE INDEX IF NOT EXISTS attempts_active_idx ON attempts(state, updated_at);
CREATE TABLE IF NOT EXISTS attempt_resource_reservations (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  site_id TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  reserved_at REAL NOT NULL,
  released_at REAL
) STRICT;
CREATE TABLE IF NOT EXISTS task_leases (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
  fencing_token INTEGER NOT NULL,
  controller_epoch INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  PRIMARY KEY(run_id, task_id),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS external_handles (
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  handle_json TEXT NOT NULL,
  attached_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS artifact_recipes (
  recipe_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  output_name TEXT NOT NULL,
  recipe_json TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS staged_artifacts (
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  state TEXT NOT NULL CHECK(state IN ('STAGED','VALIDATED','REJECTED')),
  staging_path TEXT NOT NULL,
  inventory_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  PRIMARY KEY(attempt_id, recipe_id)
) STRICT;
CREATE TABLE IF NOT EXISTS validation_records (
  validation_id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  validator_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  report_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(attempt_id, recipe_id, content_sha256)
) STRICT;
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  content_sha256 TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(recipe_id, content_sha256)
) STRICT;
CREATE TABLE IF NOT EXISTS task_commits (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  fencing_token INTEGER NOT NULL,
  artifact_set_sha256 TEXT NOT NULL,
  committed_at REAL NOT NULL,
  PRIMARY KEY(run_id, task_id),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS artifact_commits (
  run_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  task_id TEXT NOT NULL,
  output_name TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  validation_id TEXT NOT NULL REFERENCES validation_records(validation_id),
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  fencing_token INTEGER NOT NULL,
  committed_at REAL NOT NULL,
  PRIMARY KEY(run_id, recipe_id),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS task_output_slots (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  output_name TEXT NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  recipe_id TEXT NOT NULL REFERENCES artifact_recipes(recipe_id),
  PRIMARY KEY(run_id, task_id, output_name),
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
) STRICT;
CREATE TABLE IF NOT EXISTS wake_conditions (
  wake_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  attempt_id TEXT,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  expected_state_version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('ACTIVE','CONSUMED','CANCELLED')),
  not_before REAL,
  deadline REAL,
  next_check REAL,
  created_at REAL NOT NULL,
  consumed_at REAL,
  FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id),
  UNIQUE(run_id, task_id, kind, subject, expected_state_version)
) STRICT;
CREATE INDEX IF NOT EXISTS wakes_due_idx
  ON wake_conditions(status, next_check, not_before);
CREATE TABLE IF NOT EXISTS control_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  run_id TEXT,
  aggregate_type TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  old_state TEXT,
  new_state TEXT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS catalog_outbox (
  run_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','PROJECTED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  PRIMARY KEY(run_id, recipe_id)
) STRICT;
CREATE TABLE IF NOT EXISTS artifact_catalog_projection (
  run_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  output_name TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  manifest_path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  projected_at REAL NOT NULL,
  PRIMARY KEY(run_id, recipe_id),
  FOREIGN KEY(run_id, recipe_id)
    REFERENCES artifact_commits(run_id, recipe_id),
  FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
) STRICT;
CREATE TABLE IF NOT EXISTS cube_projection_outbox (
  run_id TEXT NOT NULL,
  recipe_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('PENDING','PROJECTED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  projection_id TEXT,
  entry_id TEXT,
  created_at REAL NOT NULL,
  projected_at REAL,
  PRIMARY KEY(run_id, recipe_id),
  FOREIGN KEY(run_id, recipe_id)
    REFERENCES artifact_commits(run_id, recipe_id),
  FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id)
) STRICT;
CREATE INDEX IF NOT EXISTS cube_projection_outbox_state
  ON cube_projection_outbox(state, created_at, run_id, recipe_id);
""".replace(") STRICT;", ");")


_TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.WAITING: {TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED},
    TaskState.BLOCKED: {TaskState.WAITING, TaskState.READY, TaskState.CANCELLED},
    TaskState.READY: {
        TaskState.RUNNING, TaskState.BLOCKED, TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.RUNNING: {
        TaskState.VALIDATING, TaskState.LOST, TaskState.RETRY_WAIT,
        TaskState.FAILED, TaskState.CANCELLED,
    },
    TaskState.VALIDATING: {
        TaskState.COMMITTING, TaskState.RETRY_WAIT,
        TaskState.INVALID_OUTPUT, TaskState.CANCELLED,
    },
    TaskState.COMMITTING: {
        TaskState.SUCCEEDED, TaskState.RETRY_WAIT, TaskState.FAILED,
    },
    TaskState.LOST: {TaskState.RETRY_WAIT, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RETRY_WAIT: {TaskState.READY, TaskState.CANCELLED},
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
    TaskState.INVALID_OUTPUT: set(),
}

_ATTEMPT_TRANSITIONS: dict[AttemptState, set[AttemptState]] = {
    AttemptState.CREATED: {
        AttemptState.SUBMITTING, AttemptState.FAILED, AttemptState.CANCELLED},
    AttemptState.SUBMITTING: {
        AttemptState.SUBMITTED, AttemptState.SUBMISSION_UNKNOWN,
        AttemptState.FAILED, AttemptState.CANCELLED},
    AttemptState.SUBMISSION_UNKNOWN: {AttemptState.SUBMITTED, AttemptState.LOST},
    AttemptState.SUBMITTED: {
        AttemptState.RUNNING, AttemptState.RESULT_READY,
        AttemptState.FAILED, AttemptState.LOST, AttemptState.CANCELLED},
    AttemptState.RUNNING: {
        AttemptState.RESULT_READY, AttemptState.FAILED,
        AttemptState.LOST, AttemptState.CANCELLED},
    AttemptState.RESULT_READY: {
        AttemptState.ACCEPTED, AttemptState.SUPERSEDED, AttemptState.FAILED},
    AttemptState.ACCEPTED: set(),
    AttemptState.SUPERSEDED: set(),
    AttemptState.FAILED: set(),
    AttemptState.LOST: set(),
    AttemptState.CANCELLED: set(),
}

_ACTIVE_RESERVATION_STATES = (
    AttemptState.CREATED,
    AttemptState.SUBMITTING,
    AttemptState.SUBMISSION_UNKNOWN,
    AttemptState.SUBMITTED,
    AttemptState.RUNNING,
    AttemptState.RESULT_READY,
)
_RESOURCE_ENVELOPE_FIELDS = frozenset(
    {"cpu_cores", "memory_mb", "gpus", "scratch_mb"})


def _reservation_envelope(value: dict[str, Any]) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != _RESOURCE_ENVELOPE_FIELDS:
        raise ValueError(
            "reservation envelope must declare CPU, memory, GPU, and scratch")
    result: dict[str, int] = {}
    for name in sorted(_RESOURCE_ENVELOPE_FIELDS):
        amount = value[name]
        if (isinstance(amount, bool) or not isinstance(amount, int)
                or amount < 0):
            raise ValueError(
                f"reservation envelope {name} must be a non-negative integer")
        result[name] = amount
    if result["cpu_cores"] < 1 or result["memory_mb"] < 1:
        raise ValueError(
            "reservation envelope requires at least one core and some memory")
    return result


class ControllerLock:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a+")
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            raise RuntimeError("another WorkflowController owns this runtime") from exc

    def close(self) -> None:
        if not self._stream.closed:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()


@dataclass(frozen=True)
class AttemptRecord:
    spec: AttemptSpec
    state: AttemptState
    handle: ExternalHandle | None


@dataclass(frozen=True)
class AttemptReservationRecord:
    """Durable reservation identity for one currently active attempt.

    Older attempts may have no reservation row because the Stage-8 bridge
    originally kept this state only in memory.  A restarting controller must
    adopt those attempts before dispatching anything else.
    """

    spec: AttemptSpec
    state: AttemptState
    site_id: str | None
    envelope: dict[str, int] | None
    reserved_at: float | None
    released_at: float | None


@dataclass(frozen=True)
class CommittedExternalArtifact:
    """Authoritative metadata for an artifact eligible as an external input."""

    artifact_id: str
    source_run_id: str
    recipe: ArtifactRecipe
    manifest_path: str
    content_sha256: str


class RuntimeStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(_SCHEMA)
            con.execute(
                "INSERT INTO runtime_meta(key,value) VALUES('schema','1') "
                "ON CONFLICT(key) DO NOTHING")
            schema = con.execute(
                "SELECT value FROM runtime_meta WHERE key='schema'").fetchone()
            if schema is None or schema[0] != "1":
                raise RuntimeError("unsupported Stage-1 runtime schema")

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            mode = con.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise RuntimeError(
                    "runtime database does not support required SQLite WAL mode")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("PRAGMA busy_timeout=10000")
            return con
        except BaseException:
            con.close()
            raise

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def start_controller_session(self, controller_id: str) -> int:
        if not controller_id:
            raise ValueError("controller_id cannot be empty")
        now = time.time()
        with self.transaction() as con:
            # The epoch, not the human-readable controller label, is the
            # fencing identity. Permit a restarted controller to reuse a
            # stable label while retaining every historical session row.
            con.execute(
                "UPDATE controller_sessions SET controller_id=controller_id||':'||epoch "
                "WHERE controller_id=?", (controller_id,))
            con.execute(
                "INSERT INTO controller_sessions"
                "(controller_id,started_at,heartbeat_at) VALUES(?,?,?)",
                (controller_id, now, now))
            epoch = int(con.execute(
                "SELECT epoch FROM controller_sessions WHERE controller_id=?",
                (controller_id,)).fetchone()[0])
            # The exclusive controller lock proves that this process may adopt
            # same-node attempts left by the previous controller epoch.
            con.execute("UPDATE task_leases SET controller_epoch=?", (epoch,))
            return epoch

    def stop_controller_session(self, controller_id: str) -> None:
        with self.transaction() as con:
            con.execute(
                "UPDATE controller_sessions SET stopped_at=?,heartbeat_at=? "
                "WHERE controller_id=? AND stopped_at IS NULL",
                (time.time(), time.time(), controller_id))

    def heartbeat(self, controller_id: str) -> None:
        with self.transaction() as con:
            changed = con.execute(
                "UPDATE controller_sessions SET heartbeat_at=? WHERE controller_id=? "
                "AND stopped_at IS NULL",
                (time.time(), controller_id))
            if changed.rowcount != 1:
                raise RuntimeError("controller session is not active")

    def create_run(self, graph: BoundExecutionGraph,
                   run_id: str | None = None) -> str:
        graph.validate_identity()
        run_id = run_id or uuid.uuid4().hex
        graph_json = strict_canonical_json(graph.to_dict())
        now = time.time()
        by_key = {task.key: task for task in graph.tasks}
        with self.transaction() as con:
            existing = con.execute(
                "SELECT graph_json FROM bound_graphs WHERE plan_id=?",
                (graph.plan_id,)).fetchone()
            if existing is not None and existing[0] != graph_json:
                raise RuntimeError("immutable graph identity conflict")
            con.execute(
                "INSERT INTO bound_graphs(plan_id,graph_json,graph_sha256,created_at) "
                "VALUES(?,?,?,?) ON CONFLICT(plan_id) DO NOTHING",
                (graph.plan_id, graph_json, strict_hash(graph.to_dict()), now))
            con.execute(
                "INSERT INTO runs(run_id,plan_id,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?)", (run_id, graph.plan_id,
                                      RunState.RUNNING.value, now, now))
            for task in graph.tasks:
                external = {
                    binding.input_name:
                        self._resolve_committed_external_artifact(
                            con, binding.artifact_id)
                    for binding in task.external_inputs
                    if isinstance(binding, ExternalArtifactInputBinding)
                }
                registered = {
                    binding.input_name: (
                        binding,
                        self._verify_registered_artifact(binding, task),
                    )
                    for binding in task.external_inputs
                    if isinstance(binding, RegisteredArtifactInputBinding)
                }
                unmet = len(task.inputs)
                state = TaskState.READY if unmet == 0 else TaskState.WAITING
                con.execute(
                    "INSERT INTO tasks"
                    "(run_id,task_id,task_key,task_json,state,unmet_dependencies,ready_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (run_id, task.task_id, task.key,
                     strict_canonical_json(task.to_dict()), state.value,
                     unmet, now if state is TaskState.READY else None))
                for input_name, artifact in sorted(external.items()):
                    con.execute(
                        "INSERT INTO task_external_inputs"
                        "(run_id,task_id,input_name,artifact_id,source_run_id,"
                        " recipe_id,manifest_path,content_sha256) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, task.task_id, input_name,
                         artifact.artifact_id, artifact.source_run_id,
                         artifact.recipe.recipe_id, artifact.manifest_path,
                         artifact.content_sha256))
                for input_name, (binding, record) in sorted(
                        registered.items()):
                    con.execute(
                        "INSERT INTO task_registered_inputs"
                        "(run_id,task_id,input_name,snapshot_id,record_id,"
                        " artifact_id,record_json,delivery) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, task.task_id, input_name,
                         binding.snapshot_id, record.record_id,
                         record.artifact_id,
                         strict_canonical_json(record.to_dict()),
                         binding.delivery.value))
                for recipe in task.outputs:
                    recipe_json = strict_canonical_json(
                        __import__("dataclasses").asdict(recipe))
                    existing_recipe = con.execute(
                        "SELECT recipe_json FROM artifact_recipes WHERE recipe_id=?",
                        (recipe.recipe_id,)).fetchone()
                    if existing_recipe and existing_recipe[0] != recipe_json:
                        raise RuntimeError("immutable artifact recipe conflict")
                    con.execute(
                        "INSERT INTO artifact_recipes"
                        "(recipe_id,task_id,output_name,recipe_json) VALUES(?,?,?,?) "
                        "ON CONFLICT(recipe_id) DO NOTHING",
                        (recipe.recipe_id, task.task_id,
                         recipe.output_name, recipe_json))
                self._event(con, f"run:{run_id}:task:{task.task_id}:created",
                            run_id, "task", task.task_id, None, state.value,
                            "TaskCreated", {"task_key": task.key})
            for downstream in graph.tasks:
                for binding in downstream.inputs:
                    upstream = by_key[binding.upstream_task]
                    con.execute(
                        "INSERT INTO task_dependencies"
                        "(run_id,upstream_task_id,upstream_output,"
                        " downstream_task_id,input_name) VALUES(?,?,?,?,?)",
                        (run_id, upstream.task_id, binding.upstream_output,
                         downstream.task_id, binding.input_name))
                    wake_id = strict_hash({
                        "run_id": run_id,
                        "task_id": downstream.task_id,
                        "kind": WakeKind.DEPENDENCY_COMMIT.value,
                        "input": binding.input_name,
                        "generation": 0,
                    })
                    con.execute(
                        "INSERT INTO wake_conditions"
                        "(wake_id,run_id,task_id,kind,subject,expected_state_version,"
                        " status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (wake_id, run_id, downstream.task_id,
                         WakeKind.DEPENDENCY_COMMIT.value,
                         f"{upstream.task_id}:{binding.upstream_output}",
                         0, "ACTIVE", now))
            self._event(con, f"run:{run_id}:created", run_id, "run", run_id,
                        None, RunState.RUNNING.value, "RunCreated",
                        {"plan_id": graph.plan_id})
        return run_id

    def load_graph_for_run(self, run_id: str) -> BoundExecutionGraph:
        with self.connect() as con:
            row = con.execute(
                "SELECT g.graph_json FROM runs r JOIN bound_graphs g "
                "ON g.plan_id=r.plan_id WHERE r.run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return BoundExecutionGraph.from_dict(strict_json_loads(row[0]))

    def run_state(self, run_id: str) -> RunState:
        with self.connect() as con:
            row = con.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunState(row[0])

    def cancel_run_if_never_launched(
        self, run_id: str, expected_plan_id: str,
    ) -> RunState:
        """Prove a same-node run never launched, then cancel it durably.

        The controller file lock is the liveness proof: a live controller may
        still create an attempt and therefore prevents reconciliation.  Under
        that lock, zero attempt rows, zero commits, zero task attempt counts,
        and no leases make cancellation safe.  Replaying after a crash between
        RuntimeStore cancellation and an outer control-store release is
        idempotent.
        """
        if (not isinstance(expected_plan_id, str)
                or len(expected_plan_id) != 64
                or any(value not in "0123456789abcdef"
                       for value in expected_plan_id)):
            raise ValueError("expected_plan_id must be a SHA-256 digest")
        lock = ControllerLock(self.db_path.parent / "controller.lock")
        try:
            with self.transaction() as con:
                run = con.execute(
                    "SELECT plan_id,state FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise KeyError(run_id)
                if run[0] != expected_plan_id:
                    raise RuntimeError(
                        "unlaunched reconciliation resolved another plan")
                attempts = int(con.execute(
                    "SELECT COUNT(*) FROM attempts WHERE run_id=?", (run_id,),
                ).fetchone()[0])
                commits = int(con.execute(
                    "SELECT COUNT(*) FROM artifact_commits WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0])
                leases = int(con.execute(
                    "SELECT COUNT(*) FROM task_leases WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0])
                tasks = con.execute(
                    "SELECT task_id,state,attempt_count,current_attempt_id "
                    "FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,),
                ).fetchall()
                if (attempts or commits or leases
                        or any(int(row[2]) != 0 or row[3] is not None
                               for row in tasks)):
                    raise RuntimeError(
                        "runtime run has launch/commit evidence and cannot be "
                        "released as never launched")
                current = RunState(run[1])
                if current is RunState.RUNNING:
                    for row in tasks:
                        state = TaskState(row[1])
                        if state is TaskState.CANCELLED:
                            continue
                        if TaskState.CANCELLED not in _TASK_TRANSITIONS[state]:
                            raise RuntimeError(
                                "unlaunched run contains a non-cancellable "
                                f"task state {state.value}")
                        self._transition_task(
                            con, run_id, row[0], state, TaskState.CANCELLED,
                            "NeverLaunchedRunReconciled", {})
                    con.execute(
                        "UPDATE wake_conditions SET status='CONSUMED' "
                        "WHERE run_id=? AND status='ACTIVE'", (run_id,),
                    )
                    changed = con.execute(
                        "UPDATE runs SET state=?,state_version=state_version+1,"
                        "updated_at=? WHERE run_id=? AND state=?",
                        (RunState.CANCELLED.value, time.time(), run_id,
                         RunState.RUNNING.value),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError(
                            "unlaunched run cancellation compare-and-swap failed")
                    self._event(
                        con, f"run:{run_id}:never-launched-cancelled", run_id,
                        "run", run_id, RunState.RUNNING.value,
                        RunState.CANCELLED.value,
                        "NeverLaunchedRunReconciled", {})
                    return RunState.CANCELLED
                if current in {RunState.CANCELLED, RunState.FAILED}:
                    return current
                raise RuntimeError(
                    f"run state {current.value} cannot prove never-launched "
                    "reconciliation")
        finally:
            lock.close()

    def task_state(self, run_id: str, task_id: str) -> TaskState:
        with self.connect() as con:
            row = con.execute(
                "SELECT state FROM tasks WHERE run_id=? AND task_id=?",
                (run_id, task_id)).fetchone()
        if row is None:
            raise KeyError((run_id, task_id))
        return TaskState(row[0])

    def task_rows(self, run_id: str) -> list[sqlite3.Row]:
        with self.connect() as con:
            return list(con.execute(
                "SELECT * FROM tasks WHERE run_id=? ORDER BY task_id", (run_id,)))

    def latest_attempt_stage_dir(self, run_id: str,
                                 task_id: str) -> str | None:
        """Stage directory of the most recent attempt for one task."""
        with self.connect() as con:
            row = con.execute(
                "SELECT stage_dir FROM attempts WHERE run_id=? AND task_id=? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 1",
                (run_id, task_id)).fetchone()
        return None if row is None else row[0]

    def ready_tasks(self, run_id: str, now: float | None = None) -> list[BoundTask]:
        now = time.time() if now is None else now
        with self.connect() as con:
            rows = con.execute(
                "SELECT task_json FROM tasks WHERE run_id=? AND state=? "
                "AND (ready_at IS NULL OR ready_at<=?) ORDER BY ready_at,task_id",
                (run_id, TaskState.READY.value, now)).fetchall()
        return [BoundTask.from_dict(strict_json_loads(row[0])) for row in rows]

    def ready_task_identities(self) -> frozenset[tuple[str, str]]:
        """Global READY set used to bound the controller's aging cache."""
        with self.connect() as con:
            rows = con.execute(
                "SELECT run_id,task_id FROM tasks WHERE state=?",
                (TaskState.READY.value,),
            ).fetchall()
        return frozenset((row[0], row[1]) for row in rows)

    def fail_ready_task_preflight(self, run_id: str, task_id: str,
                                  error: str) -> None:
        """Reject an infeasible READY task before creating an attempt."""
        if not isinstance(error, str) or not error:
            raise ValueError("preflight failure requires an explanation")
        with self.transaction() as con:
            self._transition_task(
                con, run_id, task_id, TaskState.READY, TaskState.FAILED,
                "TaskPreflightFailed", {"error": error})
            con.execute(
                "UPDATE tasks SET error=?,ready_at=NULL WHERE run_id=? AND task_id=?",
                (error, run_id, task_id))
            con.execute(
                "UPDATE wake_conditions SET status='CANCELLED',consumed_at=? "
                "WHERE run_id=? AND task_id=? AND status='ACTIVE'",
                (time.time(), run_id, task_id))

    def active_attempt_count(self, run_id: str) -> int:
        active = tuple(state.value for state in _ACTIVE_RESERVATION_STATES)
        placeholders = ",".join("?" for _ in active)
        with self.connect() as con:
            return int(con.execute(
                f"SELECT COUNT(*) FROM attempts WHERE run_id=? AND state IN ({placeholders})",
                (run_id, *active)).fetchone()[0])

    def active_attempt_reservations(self) -> tuple[AttemptReservationRecord, ...]:
        """All durable attempts that must consume capacity before dispatch.

        This query is deliberately global across runs.  A controller can tick
        one run while another run still owns a local process, and capacity is
        a property of the node rather than of whichever run was ticked last.
        """
        active = tuple(state.value for state in _ACTIVE_RESERVATION_STATES)
        placeholders = ",".join("?" for _ in active)
        with self.connect() as con:
            rows = con.execute(
                "SELECT a.spec_json,a.state,r.site_id,r.envelope_json,"
                "r.reserved_at,r.released_at FROM attempts a "
                "LEFT JOIN attempt_resource_reservations r "
                "ON r.attempt_id=a.attempt_id "
                f"WHERE a.state IN ({placeholders}) "
                "ORDER BY a.created_at,a.attempt_id",
                active,
            ).fetchall()
        result = []
        for row in rows:
            envelope = None
            if row[3] is not None:
                decoded = strict_json_loads(row[3])
                envelope = _reservation_envelope(decoded)
            result.append(AttemptReservationRecord(
                spec=AttemptSpec.from_dict(strict_json_loads(row[0])),
                state=AttemptState(row[1]),
                site_id=row[2],
                envelope=envelope,
                reserved_at=row[4],
                released_at=row[5],
            ))
        return tuple(result)

    def adopt_attempt_reservation(
            self, attempt_id: str, site_id: str,
            envelope: dict[str, Any],
    ) -> None:
        """Durably bind a pre-Stage-8R active attempt to reconstructed capacity."""
        if not isinstance(site_id, str) or not site_id.strip():
            raise ValueError("attempt reservation site cannot be empty")
        canonical = _reservation_envelope(envelope)
        active = tuple(state.value for state in _ACTIVE_RESERVATION_STATES)
        placeholders = ",".join("?" for _ in active)
        now = time.time()
        with self.transaction() as con:
            row = con.execute(
                f"SELECT state FROM attempts WHERE attempt_id=? "
                f"AND state IN ({placeholders})",
                (attempt_id, *active),
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "cannot adopt a reservation for a non-active attempt")
            existing = con.execute(
                "SELECT site_id,envelope_json,released_at "
                "FROM attempt_resource_reservations WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            encoded = strict_canonical_json(canonical)
            if existing is not None:
                if (existing[0] != site_id or existing[1] != encoded
                        or existing[2] is not None):
                    raise RuntimeError(
                        "durable attempt reservation identity conflict")
                return
            con.execute(
                "INSERT INTO attempt_resource_reservations"
                "(attempt_id,site_id,envelope_json,reserved_at) "
                "VALUES(?,?,?,?)",
                (attempt_id, site_id, encoded, now),
            )

    def release_attempt_reservation(self, attempt_id: str) -> None:
        """Mark a durable reservation released after its attempt is terminal."""
        with self.transaction() as con:
            row = con.execute(
                "SELECT state FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if AttemptState(row[0]) in _ACTIVE_RESERVATION_STATES:
                raise RuntimeError(
                    "cannot release capacity while its attempt is active")
            con.execute(
                "UPDATE attempt_resource_reservations "
                "SET released_at=COALESCE(released_at,?) WHERE attempt_id=?",
                (time.time(), attempt_id),
            )

    def sweep_terminal_attempt_reservations(self) -> tuple[str, ...]:
        """Close the crash window after terminal state but before release.

        Attempt state is authoritative.  Therefore an unreleased reservation
        whose attempt is already terminal cannot consume node capacity after a
        restart.  The update is idempotent and records one common recovery
        timestamp for the exact set repaired by this sweep.
        """
        active = tuple(state.value for state in _ACTIVE_RESERVATION_STATES)
        placeholders = ",".join("?" for _ in active)
        with self.transaction() as con:
            rows = con.execute(
                "SELECT r.attempt_id FROM attempt_resource_reservations r "
                "JOIN attempts a ON a.attempt_id=r.attempt_id "
                "WHERE r.released_at IS NULL "
                f"AND a.state NOT IN ({placeholders}) "
                "ORDER BY r.attempt_id",
                active,
            ).fetchall()
            attempt_ids = tuple(row[0] for row in rows)
            if attempt_ids:
                repaired = ",".join("?" for _ in attempt_ids)
                con.execute(
                    "UPDATE attempt_resource_reservations SET released_at=? "
                    f"WHERE released_at IS NULL AND attempt_id IN ({repaired})",
                    (time.time(), *attempt_ids),
                )
        return attempt_ids

    def create_attempt(self, run_id: str, task: BoundTask,
                       deployment_id: str, provider: str, stage_dir: str,
                       controller_epoch: int, *,
                       reservation_site_id: str | None = None,
                       reservation_envelope: dict[str, Any] | None = None,
                       ) -> AttemptSpec:
        now = time.time()
        if not isinstance(provider, str) or not provider:
            raise ValueError("attempt provider cannot be empty")
        if not isinstance(stage_dir, str) or not Path(stage_dir).is_absolute():
            raise ValueError("attempt stage_dir must be an absolute path")
        if (reservation_site_id is None) != (reservation_envelope is None):
            raise ValueError(
                "attempt reservation site and envelope must be supplied together")
        durable_envelope = None
        if reservation_site_id is not None:
            if not isinstance(reservation_site_id, str) \
                    or not reservation_site_id.strip():
                raise ValueError("attempt reservation site cannot be empty")
            assert reservation_envelope is not None
            durable_envelope = _reservation_envelope(reservation_envelope)
        with self.transaction() as con:
            run = con.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            if RunState(run[0]) is not RunState.RUNNING:
                raise RuntimeError("cannot create an attempt for a terminal run")
            session = con.execute(
                "SELECT stopped_at FROM controller_sessions WHERE epoch=?",
                (controller_epoch,)).fetchone()
            if session is None or session[0] is not None:
                raise RuntimeError("attempt requires an active controller epoch")
            row = con.execute(
                "SELECT state,state_version,fence_counter,attempt_count,task_json "
                "FROM tasks WHERE run_id=? AND task_id=?",
                (run_id, task.task_id)).fetchone()
            if row is None or TaskState(row[0]) is not TaskState.READY:
                raise RuntimeError("task is not ready for an attempt")
            if strict_canonical_json(task.to_dict()) != row[4]:
                raise RuntimeError("task binding changed after graph registration")
            attempt_number = int(row[3]) + 1
            if attempt_number > task.max_attempts:
                raise RuntimeError("task retry budget exhausted")
            fence = int(row[2]) + 1
            aid = make_attempt_id(
                run_id, task.task_id, deployment_id, attempt_number, fence)
            spec = AttemptSpec(
                run_id=run_id,
                deployment_id=deployment_id,
                task=task,
                attempt_id=aid,
                attempt_number=attempt_number,
                fencing_token=fence,
                provider=provider,
                input_artifacts=self._input_artifact_receipts(
                    con, run_id, task.task_id),
                stage_dir=stage_dir,
                created_at=now,
            )
            con.execute(
                "INSERT INTO attempts"
                "(attempt_id,run_id,task_id,attempt_number,fencing_token,attempt_token,"
                " deployment_id,spec_json,provider,stage_dir,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, run_id, task.task_id, attempt_number, fence,
                 spec.attempt_token, deployment_id,
                 strict_canonical_json(spec.to_dict()), provider, stage_dir,
                 AttemptState.CREATED.value, now, now))
            if reservation_site_id is not None:
                assert durable_envelope is not None
                con.execute(
                    "INSERT INTO attempt_resource_reservations"
                    "(attempt_id,site_id,envelope_json,reserved_at) "
                    "VALUES(?,?,?,?)",
                    (aid, reservation_site_id,
                     strict_canonical_json(durable_envelope), now))
            self._transition_task(
                con, run_id, task.task_id, TaskState.READY,
                TaskState.RUNNING, "AttemptCreated", {"attempt_id": aid})
            con.execute(
                "UPDATE tasks SET fence_counter=?,attempt_count=?,current_attempt_id=? "
                "WHERE run_id=? AND task_id=?",
                (fence, attempt_number, aid, run_id, task.task_id))
            con.execute(
                "INSERT INTO task_leases"
                "(run_id,task_id,attempt_id,fencing_token,controller_epoch,expires_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, task.task_id, aid, fence, controller_epoch,
                 now + task.resources.walltime_s + 30.0))
            self._insert_attempt_wakes(con, spec, int(row[1]) + 1)
            self._event(con, f"attempt:{aid}:created", run_id, "attempt", aid,
                        None, AttemptState.CREATED.value, "AttemptCreated",
                        {"attempt_number": attempt_number, "fence": fence})
            return spec

    def mark_submitting(self, attempt_id: str) -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT state FROM attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if AttemptState(row[0]) is AttemptState.SUBMITTING:
                return
            self._transition_attempt(
                con, attempt_id, AttemptState.CREATED,
                AttemptState.SUBMITTING, "SubmissionIntentPersisted", {})

    def attach_handle(self, attempt_id: str, handle: ExternalHandle) -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT run_id,state,attempt_token,provider FROM attempts "
                "WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            if handle.attempt_id != attempt_id or handle.attempt_token != row[2]:
                raise RuntimeError("provider returned a mismatched external handle")
            if handle.provider != row[3]:
                raise RuntimeError("provider returned a handle for another provider")
            existing = con.execute(
                "SELECT handle_json FROM external_handles WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            encoded = strict_canonical_json(handle.to_dict())
            if existing is not None and existing[0] != encoded:
                raise RuntimeError("external handle identity conflict")
            con.execute(
                "INSERT INTO external_handles"
                "(attempt_id,provider,external_id,handle_json,attached_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING",
                (attempt_id, handle.provider, handle.external_id, encoded, time.time()))
            state = AttemptState(row[1])
            allowed = {
                AttemptState.SUBMITTING, AttemptState.SUBMISSION_UNKNOWN,
                AttemptState.SUBMITTED, AttemptState.RUNNING,
                AttemptState.RESULT_READY,
            }
            if state not in allowed:
                raise RuntimeError(
                    f"cannot attach a handle while attempt is {state.value}")
            if state in {AttemptState.SUBMITTING, AttemptState.SUBMISSION_UNKNOWN}:
                self._transition_attempt(
                    con, attempt_id, state, AttemptState.SUBMITTED,
                    "ExternalHandleAttached", {"external_id": handle.external_id})

    def mark_submission_unknown(self, attempt_id: str, error: str) -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            state = AttemptState(row[0])
            if state is AttemptState.SUBMITTING:
                self._transition_attempt(
                    con, attempt_id, AttemptState.SUBMITTING,
                    AttemptState.SUBMISSION_UNKNOWN,
                    "SubmissionOutcomeUnknown", {"error": error})
                con.execute("UPDATE attempts SET error=? WHERE attempt_id=?",
                            (error, attempt_id))
            elif state is AttemptState.SUBMISSION_UNKNOWN:
                con.execute("UPDATE attempts SET error=?,updated_at=? "
                            "WHERE attempt_id=?",
                            (error, time.time(), attempt_id))
                self._reschedule_reconcile(con, attempt_id)
            else:
                raise RuntimeError(
                    f"submission is no longer uncertain; attempt is {state.value}")

    def active_attempts(self, run_id: str, *, due_only: bool = False,
                        now: float | None = None) -> list[AttemptRecord]:
        active = tuple(state.value for state in (
            AttemptState.SUBMITTING, AttemptState.SUBMISSION_UNKNOWN,
            AttemptState.SUBMITTED, AttemptState.RUNNING))
        placeholders = ",".join("?" for _ in active)
        params: list[Any] = [run_id, *active]
        sql = (
            "SELECT a.spec_json,a.state,h.handle_json FROM attempts a "
            "LEFT JOIN external_handles h ON h.attempt_id=a.attempt_id "
            f"WHERE a.run_id=? AND a.state IN ({placeholders})")
        if due_only:
            sql += (
                " AND EXISTS(SELECT 1 FROM wake_conditions w "
                "WHERE w.attempt_id=a.attempt_id AND w.kind=? AND w.status='ACTIVE' "
                "AND w.next_check<=?)")
            params.extend((WakeKind.ATTEMPT_RECONCILIATION.value,
                           time.time() if now is None else now))
        with self.connect() as con:
            rows = con.execute(sql, params).fetchall()
        return [AttemptRecord(
            spec=AttemptSpec.from_dict(strict_json_loads(row[0])),
            state=AttemptState(row[1]),
            handle=(ExternalHandle.from_dict(strict_json_loads(row[2]))
                    if row[2] else None),
        ) for row in rows]

    def result_ready_attempts(self, run_id: str) -> list[AttemptRecord]:
        with self.connect() as con:
            rows = con.execute(
                "SELECT a.spec_json,a.state,h.handle_json FROM attempts a "
                "LEFT JOIN external_handles h ON h.attempt_id=a.attempt_id "
                "JOIN tasks t ON t.run_id=a.run_id AND t.task_id=a.task_id "
                "WHERE a.run_id=? AND t.current_attempt_id=a.attempt_id "
                "AND a.state=? AND t.state IN (?,?)",
                (run_id, AttemptState.RESULT_READY.value,
                 TaskState.VALIDATING.value, TaskState.COMMITTING.value)).fetchall()
        seen: set[str] = set()
        result: list[AttemptRecord] = []
        for row in rows:
            spec = AttemptSpec.from_dict(strict_json_loads(row[0]))
            if spec.attempt_id in seen:
                continue
            seen.add(spec.attempt_id)
            result.append(AttemptRecord(
                spec, AttemptState(row[1]),
                ExternalHandle.from_dict(strict_json_loads(row[2]))
                if row[2] else None))
        return result

    def apply_observation(self, observation: ProviderObservation) -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT run_id,task_id,state,attempt_token,provider FROM attempts "
                "WHERE attempt_id=?", (observation.attempt_id,)).fetchone()
            if row is None:
                raise KeyError(observation.attempt_id)
            run_id, task_id = row[0], row[1]
            if observation.attempt_token != row[3]:
                raise RuntimeError("provider observation token mismatch")
            current = AttemptState(row[2])
            event_key = strict_hash({
                "attempt_id": observation.attempt_id,
                "state": observation.state.value,
                "result": observation.result_manifest_path,
                "error": observation.error,
                "exit": observation.exit_code,
            })
            if current in {
                AttemptState.ACCEPTED, AttemptState.SUPERSEDED,
                AttemptState.FAILED, AttemptState.LOST, AttemptState.CANCELLED,
            }:
                self._event(con, f"late:{event_key}", run_id, "attempt",
                            observation.attempt_id, current.value, current.value,
                            "LateProviderObservation", {
                                "observed_state": observation.state.value,
                                "disposition": "STALE_FENCE",
                            })
                return
            recovered = observation.recovered_handle
            if recovered is not None:
                if (recovered.attempt_id != observation.attempt_id
                        or recovered.attempt_token != row[3]
                        or recovered.provider != row[4]):
                    raise RuntimeError("recovered external handle identity mismatch")
                encoded = strict_canonical_json(
                    recovered.to_dict())
                existing_handle = con.execute(
                    "SELECT handle_json FROM external_handles WHERE attempt_id=?",
                    (observation.attempt_id,)).fetchone()
                if existing_handle is not None and existing_handle[0] != encoded:
                    raise RuntimeError("recovered external handle identity conflict")
                con.execute(
                    "INSERT INTO external_handles"
                    "(attempt_id,provider,external_id,handle_json,attached_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING",
                    (observation.attempt_id, recovered.provider,
                     recovered.external_id,
                     encoded, time.time()))
            target = observation.state
            if target in {AttemptState.CREATED, AttemptState.SUBMITTING,
                          AttemptState.SUBMITTED, AttemptState.ACCEPTED,
                          AttemptState.SUPERSEDED}:
                raise RuntimeError(
                    f"provider may not report controller-owned state "
                    f"{target.value}")
            if target is current:
                if target is AttemptState.RUNNING:
                    self._reschedule_reconcile(con, observation.attempt_id)
                    return
                if target is AttemptState.RESULT_READY:
                    persisted_result = con.execute(
                        "SELECT result_manifest_path FROM attempts "
                        "WHERE attempt_id=?", (observation.attempt_id,),
                    ).fetchone()[0]
                    if persisted_result != observation.result_manifest_path:
                        raise RuntimeError(
                            "duplicate completion changed its result manifest")
                    return
            if (current in {AttemptState.SUBMITTING,
                            AttemptState.SUBMISSION_UNKNOWN}
                    and target in {AttemptState.RUNNING,
                                   AttemptState.RESULT_READY,
                                   AttemptState.FAILED,
                                   AttemptState.CANCELLED}
                    and recovered is not None):
                self._transition_attempt(
                    con, observation.attempt_id, current,
                    AttemptState.SUBMITTED, "SubmissionRecovered", {
                        "external_id": recovered.external_id})
                current = AttemptState.SUBMITTED
            if target is AttemptState.RUNNING and current is AttemptState.SUBMITTED:
                self._transition_attempt(
                    con, observation.attempt_id, current,
                    AttemptState.RUNNING, "AttemptRunning", {})
                self._reschedule_reconcile(con, observation.attempt_id)
                return
            if (target is AttemptState.SUBMISSION_UNKNOWN
                    and current in {AttemptState.SUBMITTING,
                                    AttemptState.SUBMISSION_UNKNOWN}):
                if current is AttemptState.SUBMITTING:
                    self._transition_attempt(
                        con, observation.attempt_id, current, target,
                        "SubmissionOutcomeUnknown", {"error": observation.error})
                self._reschedule_reconcile(con, observation.attempt_id)
                return
            if target is AttemptState.RESULT_READY and current in {
                    AttemptState.SUBMITTED, AttemptState.RUNNING}:
                self._transition_attempt(
                    con, observation.attempt_id, current, target,
                    "AttemptResultReady", {
                        "result_manifest_path": observation.result_manifest_path})
                con.execute(
                    "UPDATE attempts SET result_manifest_path=? WHERE attempt_id=?",
                    (observation.result_manifest_path, observation.attempt_id))
                task_current = TaskState(con.execute(
                    "SELECT state FROM tasks WHERE run_id=? AND task_id=?",
                    (run_id, task_id)).fetchone()[0])
                if task_current is TaskState.RUNNING:
                    self._transition_task(
                        con, run_id, task_id, task_current,
                        TaskState.VALIDATING, "ValidationReady", {})
                self._consume_attempt_wakes(con, observation.attempt_id)
                return
            if target in {AttemptState.FAILED, AttemptState.LOST,
                          AttemptState.CANCELLED}:
                if target not in _ATTEMPT_TRANSITIONS[current]:
                    raise RuntimeError(f"illegal observation {current.value}->{target.value}")
                self._transition_attempt(
                    con, observation.attempt_id, current, target,
                    f"Attempt{target.value.title()}", {"error": observation.error})
                con.execute(
                    "UPDATE attempts SET error=? WHERE attempt_id=?",
                    (observation.error, observation.attempt_id))
                self._consume_attempt_wakes(con, observation.attempt_id)
                self._fail_or_retry(con, run_id, task_id,
                                    observation.attempt_id, target,
                                    observation.error)
                return
            raise RuntimeError(
                f"unsupported provider observation {current.value}->{target.value}")

    def due_deadline_attempts(self, run_id: str,
                              now: float | None = None) -> list[AttemptRecord]:
        now = time.time() if now is None else now
        ids: list[str]
        with self.connect() as con:
            ids = [row[0] for row in con.execute(
                "SELECT attempt_id FROM wake_conditions WHERE run_id=? "
                "AND kind=? AND status='ACTIVE' AND deadline<=?",
                (run_id, WakeKind.DEADLINE.value, now))]
        records = {r.spec.attempt_id: r for r in self.active_attempts(run_id)}
        return [records[aid] for aid in ids if aid in records]

    def consume_due_retry_wakes(self, run_id: str,
                                now: float | None = None) -> int:
        now = time.time() if now is None else now
        count = 0
        with self.transaction() as con:
            wakes = con.execute(
                "SELECT * FROM wake_conditions WHERE run_id=? AND kind=? "
                "AND status='ACTIVE' AND not_before<=? ORDER BY not_before,wake_id",
                (run_id, WakeKind.RETRY_TIMER.value, now)).fetchall()
            for wake in wakes:
                task = con.execute(
                    "SELECT state,state_version FROM tasks WHERE run_id=? AND task_id=?",
                    (run_id, wake["task_id"])).fetchone()
                if (task is not None
                        and TaskState(task[0]) is TaskState.RETRY_WAIT
                        and int(task[1]) == int(wake["expected_state_version"])):
                    self._transition_task(
                        con, run_id, wake["task_id"], TaskState.RETRY_WAIT,
                        TaskState.READY, "RetryTimerFired", {})
                    con.execute(
                        "UPDATE tasks SET ready_at=? WHERE run_id=? AND task_id=?",
                        (now, run_id, wake["task_id"]))
                    count += 1
                con.execute(
                    "UPDATE wake_conditions SET status='CONSUMED',consumed_at=? "
                    "WHERE wake_id=?", (now, wake["wake_id"]))
        return count

    def begin_committing(self, run_id: str, task_id: str) -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT state,current_attempt_id,fence_counter FROM tasks "
                "WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
            if row is None:
                raise KeyError((run_id, task_id))
            state = TaskState(row[0])
            if state is TaskState.VALIDATING:
                attempt = con.execute(
                    "SELECT state,fencing_token FROM attempts "
                    "WHERE attempt_id=? AND run_id=? AND task_id=?",
                    (row[1], run_id, task_id)).fetchone()
                if (attempt is None
                        or AttemptState(attempt[0]) is not AttemptState.RESULT_READY
                        or int(attempt[1]) != int(row[2])):
                    raise RuntimeError(
                        "commit requires the current fenced RESULT_READY attempt")
                expected = int(con.execute(
                    "SELECT COUNT(*) FROM artifact_recipes WHERE task_id=?",
                    (task_id,)).fetchone()[0])
                valid = int(con.execute(
                    "SELECT COUNT(DISTINCT v.recipe_id) FROM validation_records v "
                    "JOIN staged_artifacts s ON s.attempt_id=v.attempt_id "
                    "AND s.recipe_id=v.recipe_id AND s.content_sha256=v.content_sha256 "
                    "WHERE v.attempt_id=? AND v.passed=1 AND s.state='VALIDATED'",
                    (row[1],)).fetchone()[0])
                if expected < 1 or valid != expected:
                    raise RuntimeError(
                        "commit requires passed validation for every output")
                self._transition_task(
                    con, run_id, task_id, state, TaskState.COMMITTING,
                    "ValidationPassed", {})
            elif state is not TaskState.COMMITTING:
                raise RuntimeError(f"cannot commit task in {state.value}")

    def mark_invalid_output(self, run_id: str, task_id: str,
                            attempt_id: str, error: str) -> None:
        with self.transaction() as con:
            task = con.execute(
                "SELECT state,current_attempt_id,fence_counter FROM tasks "
                "WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
            attempt = con.execute(
                "SELECT state,run_id,task_id,fencing_token FROM attempts "
                "WHERE attempt_id=?", (attempt_id,)).fetchone()
            if task is None:
                raise KeyError((run_id, task_id))
            if attempt is None:
                raise KeyError(attempt_id)
            if (task[1] != attempt_id
                    or attempt[1] != run_id or attempt[2] != task_id
                    or int(task[2]) != int(attempt[3])):
                raise RuntimeError("invalid output does not belong to current fence")
            task_state = TaskState(task[0])
            if task_state is TaskState.VALIDATING:
                self._transition_task(
                    con, run_id, task_id, task_state,
                    TaskState.INVALID_OUTPUT, "ScientificValidationFailed",
                    {"error": error})
            attempt_state = AttemptState(attempt[0])
            if attempt_state is AttemptState.RESULT_READY:
                self._transition_attempt(
                    con, attempt_id, attempt_state, AttemptState.FAILED,
                    "OutputRejected", {"error": error})
            con.execute("DELETE FROM task_leases WHERE attempt_id=?", (attempt_id,))
            con.execute(
                "UPDATE tasks SET error=? WHERE run_id=? AND task_id=?",
                (error, run_id, task_id))
            con.execute(
                "UPDATE attempts SET error=? WHERE attempt_id=?",
                (error, attempt_id))
            self._consume_attempt_wakes(con, attempt_id)

    def finalize_run_state(self, run_id: str) -> RunState:
        with self.transaction() as con:
            run = con.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            current = RunState(run[0])
            if current is not RunState.RUNNING:
                return current
            states = [TaskState(row[0]) for row in con.execute(
                "SELECT state FROM tasks WHERE run_id=?", (run_id,))]
            target: RunState | None = None
            if states and all(state is TaskState.SUCCEEDED for state in states):
                target = RunState.SUCCEEDED
            elif any(state in {TaskState.FAILED, TaskState.INVALID_OUTPUT,
                               TaskState.LOST} for state in states):
                target = RunState.FAILED
            elif any(state is TaskState.CANCELLED for state in states):
                target = RunState.CANCELLED
            if target is not None:
                con.execute(
                    "UPDATE runs SET state=?,state_version=state_version+1,updated_at=? "
                    "WHERE run_id=? AND state=?",
                    (target.value, time.time(), run_id, current.value))
                self._event(con, f"run:{run_id}:{target.value}", run_id,
                            "run", run_id, current.value, target.value,
                            f"Run{target.value.title()}", {})
                return target
            return current

    def request_cancel(self, run_id: str, task_id: str) -> AttemptRecord | None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT state,current_attempt_id FROM tasks WHERE run_id=? AND task_id=?",
                (run_id, task_id)).fetchone()
            if row is None:
                raise KeyError((run_id, task_id))
            state = TaskState(row[0])
            if state in {TaskState.SUCCEEDED, TaskState.FAILED,
                         TaskState.INVALID_OUTPUT, TaskState.CANCELLED}:
                return None
            if TaskState.CANCELLED not in _TASK_TRANSITIONS[state]:
                raise RuntimeError(
                    f"cannot cancel task while it is {state.value}; "
                    "finish or recover the authoritative commit first")
            aid = row[1]
            con.execute(
                "UPDATE tasks SET fence_counter=fence_counter+1 WHERE run_id=? AND task_id=?",
                (run_id, task_id))
            self._transition_task(
                con, run_id, task_id, state, TaskState.CANCELLED,
                "CancellationRequested", {})
            con.execute(
                "UPDATE wake_conditions SET status='CANCELLED',consumed_at=? "
                "WHERE run_id=? AND task_id=? AND status='ACTIVE'",
                (time.time(), run_id, task_id))
        if not aid:
            return None
        return self.attempt_record(aid)

    def mark_attempt_cancelled(self, attempt_id: str, error: str = "cancelled") -> None:
        with self.transaction() as con:
            row = con.execute(
                "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = AttemptState(row[0])
            if AttemptState.CANCELLED in _ATTEMPT_TRANSITIONS.get(current, set()):
                self._transition_attempt(
                    con, attempt_id, current, AttemptState.CANCELLED,
                    "AttemptCancelled", {"error": error})
            con.execute("DELETE FROM task_leases WHERE attempt_id=?", (attempt_id,))

    def attempt_record(self, attempt_id: str) -> AttemptRecord:
        with self.connect() as con:
            row = con.execute(
                "SELECT a.spec_json,a.state,h.handle_json FROM attempts a "
                "LEFT JOIN external_handles h ON h.attempt_id=a.attempt_id "
                "WHERE a.attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return AttemptRecord(
            AttemptSpec.from_dict(strict_json_loads(row[0])), AttemptState(row[1]),
            ExternalHandle.from_dict(strict_json_loads(row[2]))
            if row[2] else None)

    def event_count(self, *, event_type: str | None = None) -> int:
        with self.connect() as con:
            if event_type is None:
                return int(con.execute("SELECT COUNT(*) FROM control_events").fetchone()[0])
            return int(con.execute(
                "SELECT COUNT(*) FROM control_events WHERE event_type=?",
                (event_type,)).fetchone()[0])

    def committed_output(self, run_id: str, task_id: str,
                         output_name: str = "result") -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT s.*,a.manifest_path,a.content_sha256 FROM task_output_slots s "
                "JOIN artifacts a ON a.artifact_id=s.artifact_id "
                "WHERE s.run_id=? AND s.task_id=? AND s.output_name=?",
                (run_id, task_id, output_name)).fetchone()

    def task_input_artifact_ids(
            self, run_id: str, task_id: str) -> tuple[tuple[str, str], ...]:
        """Return exact committed inputs under their runtime port names."""
        return tuple(
            (value.input_name, value.artifact_id)
            for value in self.task_input_lineage(run_id, task_id))

    def task_input_lineage(
            self, run_id: str, task_id: str) -> tuple[TaskInputLineage, ...]:
        """Return exact input identities with their authoritative namespace."""
        with self.connect() as con:
            internal = con.execute(
                "SELECT d.input_name,s.artifact_id FROM task_dependencies d "
                "JOIN task_output_slots s ON s.run_id=d.run_id "
                "AND s.task_id=d.upstream_task_id "
                "AND s.output_name=d.upstream_output "
                "WHERE d.run_id=? AND d.downstream_task_id=?",
                (run_id, task_id),
            ).fetchall()
            external = con.execute(
                "SELECT input_name,artifact_id FROM task_external_inputs "
                "WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchall()
            registered = con.execute(
                "SELECT input_name,artifact_id FROM task_registered_inputs "
                "WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchall()
        values = tuple(sorted((
            *(TaskInputLineage(
                str(row[0]), InputArtifactSource.STAGE1_COMMIT, str(row[1]))
              for row in (*internal, *external)),
            *(TaskInputLineage(
                str(row[0]), InputArtifactSource.REGISTERED_ARTIFACT,
                str(row[1])) for row in registered),
        ), key=lambda value: value.input_name))
        if len({value.input_name for value in values}) != len(values):
            raise RuntimeError("runtime task input port identity is ambiguous")
        return values

    def committed_external_artifact(
            self, artifact_id: str) -> CommittedExternalArtifact:
        """Resolve one artifact only through an authoritative Stage-1 commit.

        This is the read boundary used by partition compilation to inspect the
        frozen scientific descriptor.  Run creation repeats the lookup inside
        its own transaction, so a caller cannot substitute this result for a
        different graph binding between planning and registration.
        """
        with self.connect() as con:
            return self._resolve_committed_external_artifact(con, artifact_id)

    def project_catalog_outbox(self, *, limit: int = 100) -> int:
        """Idempotently project committed artifacts into the catalog view.

        The authoritative visibility point remains ``artifact_commits``.  This
        read model is rebuildable and is deliberately updated in the same
        transaction that consumes each outbox row, so a controller restart may
        safely repeat projection.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("catalog projection limit must be a positive integer")
        projected = 0
        with self.transaction() as con:
            rows = con.execute(
                "SELECT o.run_id,o.recipe_id,o.artifact_id,c.task_id,c.output_name,"
                " a.manifest_path,a.content_sha256 FROM catalog_outbox o "
                "JOIN artifact_commits c ON c.run_id=o.run_id "
                "AND c.recipe_id=o.recipe_id AND c.artifact_id=o.artifact_id "
                "JOIN artifacts a ON a.artifact_id=o.artifact_id "
                "WHERE o.state IN ('PENDING','FAILED') "
                "ORDER BY o.run_id,o.recipe_id LIMIT ?",
                (limit,),
            ).fetchall()
            now = time.time()
            for row in rows:
                existing = con.execute(
                    "SELECT artifact_id,task_id,output_name,manifest_path,"
                    " content_sha256 FROM artifact_catalog_projection "
                    "WHERE run_id=? AND recipe_id=?",
                    (row[0], row[1]),
                ).fetchone()
                # SELECT order above is artifact,task,output,manifest,digest;
                # the projection table presents task/output before artifact.
                projected_value = (
                    row[2], row[3], row[4], row[5], row[6])
                if existing is not None and tuple(existing) != projected_value:
                    con.execute(
                        "UPDATE catalog_outbox SET state='FAILED',attempts=attempts+1,"
                        " error=? WHERE run_id=? AND recipe_id=?",
                        ("catalog projection identity conflict", row[0], row[1]),
                    )
                    raise RuntimeError("catalog projection identity conflict")
                if existing is None:
                    con.execute(
                        "INSERT INTO artifact_catalog_projection"
                        "(run_id,recipe_id,task_id,output_name,artifact_id,"
                        " manifest_path,content_sha256,projected_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (row[0], row[1], row[3], row[4], row[2],
                         row[5], row[6], now),
                    )
                con.execute(
                    "UPDATE catalog_outbox SET state='PROJECTED',"
                    "attempts=attempts+1,error=NULL WHERE run_id=? AND recipe_id=?",
                    (row[0], row[1]),
                )
                projected += 1
        return projected

    def _input_artifact_receipts(
            self, con: sqlite3.Connection, run_id: str,
            downstream_task_id: str) -> dict[
                str, AttemptInputReceipt | RegisteredArtifactInputReceipt]:
        internal_rows = con.execute(
            "SELECT d.input_name,a.artifact_id,a.recipe_id,a.manifest_path,"
            "a.content_sha256,a.manifest_json FROM task_dependencies d "
            "JOIN task_output_slots s ON s.run_id=d.run_id "
            "AND s.task_id=d.upstream_task_id AND s.output_name=d.upstream_output "
            "JOIN artifacts a ON a.artifact_id=s.artifact_id "
            "AND a.recipe_id=s.recipe_id "
            "JOIN artifact_commits c ON c.run_id=s.run_id "
            "AND c.task_id=s.task_id AND c.output_name=s.output_name "
            "AND c.artifact_id=s.artifact_id AND c.recipe_id=s.recipe_id "
            "WHERE d.run_id=? AND d.downstream_task_id=? AND d.satisfied=1",
            (run_id, downstream_task_id)).fetchall()
        expected_internal = int(con.execute(
            "SELECT COUNT(*) FROM task_dependencies WHERE run_id=? AND downstream_task_id=?",
            (run_id, downstream_task_id)).fetchone()[0])
        if len(internal_rows) != expected_internal:
            raise RuntimeError("task became READY before all artifacts committed")
        external_rows = con.execute(
            "SELECT e.input_name,e.artifact_id,e.recipe_id,e.manifest_path,"
            "e.content_sha256,a.manifest_json "
            "FROM task_external_inputs e "
            "JOIN artifacts a ON a.artifact_id=e.artifact_id "
            "AND a.recipe_id=e.recipe_id "
            "AND a.manifest_path=e.manifest_path "
            "AND a.content_sha256=e.content_sha256 "
            "WHERE e.run_id=? AND e.task_id=? "
            "AND EXISTS (SELECT 1 FROM artifact_commits c "
            "            WHERE c.run_id=e.source_run_id "
            "            AND c.artifact_id=e.artifact_id "
            "            AND c.recipe_id=e.recipe_id)",
            (run_id, downstream_task_id)).fetchall()
        registered_rows = con.execute(
            "SELECT input_name,snapshot_id,record_id,artifact_id,record_json,"
            "delivery FROM task_registered_inputs "
            "WHERE run_id=? AND task_id=?",
            (run_id, downstream_task_id)).fetchall()
        task_row = con.execute(
            "SELECT task_json FROM tasks WHERE run_id=? AND task_id=?",
            (run_id, downstream_task_id)).fetchone()
        if task_row is None:
            raise KeyError((run_id, downstream_task_id))
        task = BoundTask.from_dict(strict_json_loads(task_row[0]))
        expected_external = {
            value.input_name: value.artifact_id
            for value in task.external_inputs
            if isinstance(value, ExternalArtifactInputBinding)
        }
        actual_external = {row[0]: row[1] for row in external_rows}
        if actual_external != expected_external:
            raise RuntimeError(
                "external artifact input binding lost authoritative commit")
        expected_registered = {
            value.input_name: value
            for value in task.external_inputs
            if isinstance(value, RegisteredArtifactInputBinding)
        }
        if set(row[0] for row in registered_rows) != set(expected_registered):
            raise RuntimeError(
                "registered artifact input binding lost its exact receipt")
        result = {
            row[0]: self._attempt_input_receipt(row)
            for row in internal_rows
        }
        result.update({
            row[0]: self._attempt_input_receipt(row)
            for row in external_rows
        })
        for row in registered_rows:
            binding = expected_registered[row[0]]
            if (row[1] != binding.snapshot_id
                    or row[2] != binding.record_id
                    or row[3] != binding.artifact_id
                    or row[4] != strict_canonical_json(binding.record)
                    or row[5] != binding.delivery.value):
                raise RuntimeError(
                    "registered artifact receipt conflicts with bound graph")
            record = self._verify_registered_artifact(binding, task)
            result[row[0]] = RegisteredArtifactInputReceipt(
                snapshot_id=binding.snapshot_id,
                record=record.to_dict(),
                delivery=binding.delivery,
            )
        if (len(result) != expected_internal + len(expected_external)
                + len(expected_registered)):
            raise RuntimeError("attempt input ports are not uniquely bound")
        return result

    @staticmethod
    def _verify_registered_artifact(
            binding: RegisteredArtifactInputBinding, task: BoundTask):
        """Replay a native registry receipt and its current exact bytes."""
        from artifacts.records import ArtifactRecord
        record = ArtifactRecord.from_dict(dict(binding.record))
        if record.media_type != "application/json":
            raise ValueError(
                "registered runtime inputs currently support application/json "
                "only")
        pointer_operation = (
            task.component.operation_key == "native.file_pointer_identity.v1")
        if binding.delivery is RegisteredArtifactDelivery.NATIVE_FILE_POINTER:
            if not pointer_operation or binding.input_name != "source":
                raise ValueError(
                    "native pointer delivery is restricted to the closed "
                    "native pointer identity source port")
        elif pointer_operation:
            raise ValueError(
                "native pointer identity requires explicit pointer delivery")
        verify_native_file(
            record.location,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
        )
        return record

    @staticmethod
    def _attempt_input_receipt(row: sqlite3.Row) -> AttemptInputReceipt:
        manifest = strict_json_loads(row[5])
        required = {
            "schema", "artifact_id", "recipe_id", "media_type",
            "content_sha256", "size_bytes", "object_path",
        }
        if (not isinstance(manifest, dict) or set(manifest) != required
                or manifest["schema"] != "stage1-artifact-manifest-v1"
                or manifest["artifact_id"] != row[1]
                or manifest["recipe_id"] != row[2]
                or manifest["content_sha256"] != row[4]):
            raise RuntimeError(
                "attempt input manifest conflicts with artifact authority")
        return AttemptInputReceipt(
            artifact_id=row[1],
            recipe_id=row[2],
            content_sha256=row[4],
            size_bytes=manifest["size_bytes"],
            manifest_path=row[3],
        )

    def _resolve_committed_external_artifact(
            self, con: sqlite3.Connection,
            artifact_id: str) -> CommittedExternalArtifact:
        row = con.execute(
            "SELECT a.artifact_id,c.run_id,a.recipe_id,r.recipe_json,"
            " a.manifest_path,a.manifest_json,a.content_sha256 "
            "FROM artifacts a "
            "JOIN artifact_recipes r ON r.recipe_id=a.recipe_id "
            "JOIN artifact_commits c ON c.artifact_id=a.artifact_id "
            "AND c.recipe_id=a.recipe_id "
            "WHERE a.artifact_id=? "
            "ORDER BY c.committed_at,c.run_id LIMIT 1",
            (artifact_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"external artifact {artifact_id!r} has no authoritative commit")
        recipe = ArtifactRecipe.from_dict(strict_json_loads(row[3]))
        if recipe.recipe_id != row[2]:
            raise RuntimeError(
                "authoritative external artifact recipe identity changed")
        manifest = strict_json_loads(row[5])
        required = {
            "schema", "artifact_id", "recipe_id", "media_type",
            "content_sha256", "size_bytes", "object_path",
        }
        if (not isinstance(manifest, dict) or set(manifest) != required
                or manifest["schema"] != "stage1-artifact-manifest-v1"
                or manifest["artifact_id"] != row[0]
                or manifest["recipe_id"] != row[2]
                or manifest["content_sha256"] != row[6]
                or manifest["media_type"] != recipe.media_type):
            raise RuntimeError(
                "authoritative external artifact manifest metadata conflicts")
        manifest_path = Path(row[4])
        if not manifest_path.is_absolute():
            raise RuntimeError(
                "authoritative external artifact manifest path is not absolute")
        try:
            on_disk = strict_json_loads(
                manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "authoritative external artifact manifest is unavailable") from exc
        if on_disk != manifest:
            raise RuntimeError(
                "authoritative external artifact manifest changed on disk")
        return CommittedExternalArtifact(
            artifact_id=row[0], source_run_id=row[1], recipe=recipe,
            manifest_path=str(manifest_path), content_sha256=row[6])

    def _insert_attempt_wakes(self, con: sqlite3.Connection,
                              spec: AttemptSpec, task_version: int) -> None:
        for kind, subject, deadline, next_check in (
            (WakeKind.ATTEMPT_RECONCILIATION,
             spec.attempt_token, None, time.time()),
            (WakeKind.DEADLINE,
             spec.attempt_token,
             time.time() + spec.task.resources.walltime_s, None),
        ):
            wake_id = strict_hash({
                "run_id": spec.run_id,
                "attempt_id": spec.attempt_id,
                "kind": kind.value,
                "generation": task_version,
            })
            con.execute(
                "INSERT INTO wake_conditions"
                "(wake_id,run_id,task_id,attempt_id,kind,subject,"
                " expected_state_version,status,deadline,next_check,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (wake_id, spec.run_id, spec.task.task_id, spec.attempt_id,
                 kind.value, subject, task_version, "ACTIVE", deadline,
                 next_check, time.time()))

    def _reschedule_reconcile(self, con: sqlite3.Connection,
                              attempt_id: str, delay: float = 0.05) -> None:
        con.execute(
            "UPDATE wake_conditions SET next_check=? WHERE attempt_id=? "
            "AND kind=? AND status='ACTIVE'",
            (time.time() + delay, attempt_id,
             WakeKind.ATTEMPT_RECONCILIATION.value))

    def _consume_attempt_wakes(self, con: sqlite3.Connection,
                               attempt_id: str) -> None:
        con.execute(
            "UPDATE wake_conditions SET status='CONSUMED',consumed_at=? "
            "WHERE attempt_id=? AND status='ACTIVE'", (time.time(), attempt_id))

    def _fail_or_retry(self, con: sqlite3.Connection, run_id: str,
                       task_id: str, attempt_id: str,
                       attempt_state: AttemptState, error: str | None) -> None:
        task_row = con.execute(
            "SELECT state,state_version,attempt_count,task_json FROM tasks "
            "WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
        current = TaskState(task_row[0])
        if current is not TaskState.RUNNING:
            return
        task = BoundTask.from_dict(strict_json_loads(task_row[3]))
        retry = (task.component.retry_safe
                 and int(task_row[2]) < task.max_attempts
                 and attempt_state in {AttemptState.FAILED, AttemptState.LOST})
        con.execute("DELETE FROM task_leases WHERE attempt_id=?", (attempt_id,))
        if retry:
            self._transition_task(
                con, run_id, task_id, current, TaskState.RETRY_WAIT,
                "RetryScheduled", {"error": error})
            row = con.execute(
                "SELECT state_version FROM tasks WHERE run_id=? AND task_id=?",
                (run_id, task_id)).fetchone()
            not_before = time.time() + task.retry_delay_s
            con.execute(
                "UPDATE tasks SET ready_at=?,error=? WHERE run_id=? AND task_id=?",
                (not_before, error, run_id, task_id))
            wake_id = strict_hash({
                "run_id": run_id, "task_id": task_id,
                "kind": WakeKind.RETRY_TIMER.value,
                "generation": int(row[0]),
            })
            con.execute(
                "INSERT INTO wake_conditions"
                "(wake_id,run_id,task_id,attempt_id,kind,subject,"
                " expected_state_version,status,not_before,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (wake_id, run_id, task_id, attempt_id,
                 WakeKind.RETRY_TIMER.value, attempt_id,
                 int(row[0]), "ACTIVE", not_before, time.time()))
        else:
            target = TaskState.LOST if attempt_state is AttemptState.LOST else (
                TaskState.CANCELLED if attempt_state is AttemptState.CANCELLED
                else TaskState.FAILED)
            self._transition_task(
                con, run_id, task_id, current, target,
                f"Task{target.value.title()}", {"error": error})
            con.execute(
                "UPDATE tasks SET error=? WHERE run_id=? AND task_id=?",
                (error, run_id, task_id))

    def _transition_task(self, con: sqlite3.Connection, run_id: str,
                         task_id: str, old: TaskState, new: TaskState,
                         event_type: str, payload: dict[str, Any]) -> int:
        if new not in _TASK_TRANSITIONS[old]:
            raise RuntimeError(f"illegal task transition {old.value}->{new.value}")
        cur = con.execute(
            "UPDATE tasks SET state=?,state_version=state_version+1 "
            "WHERE run_id=? AND task_id=? AND state=?",
            (new.value, run_id, task_id, old.value))
        if cur.rowcount != 1:
            raise RuntimeError("task state compare-and-swap failed")
        version = int(con.execute(
            "SELECT state_version FROM tasks WHERE run_id=? AND task_id=?",
            (run_id, task_id)).fetchone()[0])
        self._event(con, f"task:{run_id}:{task_id}:v{version}", run_id,
                    "task", task_id, old.value, new.value,
                    event_type, payload)
        return version

    def _transition_attempt(self, con: sqlite3.Connection, attempt_id: str,
                            old: AttemptState, new: AttemptState,
                            event_type: str, payload: dict[str, Any]) -> int:
        if new not in _ATTEMPT_TRANSITIONS[old]:
            raise RuntimeError(f"illegal attempt transition {old.value}->{new.value}")
        row = con.execute(
            "SELECT run_id,state_version FROM attempts WHERE attempt_id=? AND state=?",
            (attempt_id, old.value)).fetchone()
        if row is None:
            raise RuntimeError("attempt state compare-and-swap failed")
        version = int(row[1]) + 1
        con.execute(
            "UPDATE attempts SET state=?,state_version=?,updated_at=? "
            "WHERE attempt_id=? AND state=?",
            (new.value, version, time.time(), attempt_id, old.value))
        self._event(con, f"attempt:{attempt_id}:v{version}", row[0],
                    "attempt", attempt_id, old.value, new.value,
                    event_type, payload)
        return version

    @staticmethod
    def _event(con: sqlite3.Connection, key: str, run_id: str | None,
               aggregate_type: str, aggregate_id: str,
               old_state: str | None, new_state: str | None,
               event_type: str, payload: dict[str, Any]) -> None:
        payload_json = strict_canonical_json(payload)
        con.execute(
            "INSERT INTO control_events"
            "(idempotency_key,run_id,aggregate_type,aggregate_id,old_state,"
            " new_state,event_type,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO NOTHING",
            (key, run_id, aggregate_type, aggregate_id,
             old_state, new_state, event_type,
             payload_json, time.time()))
        persisted = con.execute(
            "SELECT run_id,aggregate_type,aggregate_id,old_state,new_state,"
            "event_type,payload_json FROM control_events "
            "WHERE idempotency_key=?", (key,)).fetchone()
        expected = (run_id, aggregate_type, aggregate_id, old_state,
                    new_state, event_type, payload_json)
        if persisted is None or tuple(persisted) != expected:
            raise RuntimeError("control event idempotency conflict")
