"""Stage-4 scientific field validation at the authoritative commit boundary."""
from __future__ import annotations

import copy

import pytest

from engine.runtime import RunState, TaskState, WorkflowController
from engine.runtime.operations import operation_component
from engine.runtime.types import BoundExecutionGraph, OutputSpec, TaskTemplate


DESCRIPTOR_ID = "d" * 64


def _configuration(*, temporal=None):
    return {
        "kind": "field_json_v1",
        "descriptor_id": DESCRIPTOR_ID,
        "crs": "EPSG:4326",
        "grid_shape": [2, 2],
        "grid_affine": ["1", "0", "0", "0", "1", "10"],
        "temporal": temporal or {
            "kind": "TIME_INVARIANT",
            "start": None,
            "end": None,
            "cadence_s": None,
        },
        "component_names": ["value"],
    }


def _field():
    return {
        "schema": "field-json-v1",
        "crs": "EPSG:4326",
        "x": [0.0, 1.0],
        "y": [10.0, 11.0],
        "time": ["TIME_INVARIANT"],
        "components": {"value": [[[1.0, 2.0], [3.0, 4.0]]]},
    }


def _graph(value, *, configuration=None):
    return BoundExecutionGraph.bind(
        "stage4-field-validator",
        (TaskTemplate(
            key="field-source",
            component=operation_component("synthetic.constant.v1"),
            parameters={"value": value},
            outputs=(OutputSpec(
                "result", "application/json",
                configuration or _configuration()),),
        ),),
        schema_version="stage4-field-validation-fixture-v1",
    )


def test_valid_field_is_semantically_validated_before_commit(tmp_path):
    graph = _graph(_field())
    task = graph.tasks[0]
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED
        assert controller.output_value(run_id, task.task_id) == _field()
        with controller.store.connect() as connection:
            validator_id, report = connection.execute(
                "SELECT validator_id,report_json FROM validation_records "
                "WHERE attempt_id IN (SELECT attempt_id FROM attempts "
                "WHERE run_id=?)", (run_id,),
            ).fetchone()
        assert validator_id == "stage4.field-json@1"
        assert '"field_json_v1":true' in report


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(crs="EPSG:3857"),
    lambda value: value.update(x=[0.0]),
    lambda value: value.update(time=[]),
    lambda value: value["components"].update(
        value=[[[1.0, 2.0]]]),
    lambda value: value.update(components={"wrong": value["components"]["value"]}),
])
def test_wrong_field_semantics_never_become_visible(tmp_path, mutation):
    value = copy.deepcopy(_field())
    mutation(value)
    graph = _graph(value)
    task = graph.tasks[0]
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.FAILED
        assert controller.store.task_state(
            run_id, task.task_id) is TaskState.INVALID_OUTPUT
        assert controller.store.committed_output(run_id, task.task_id) is None


def test_series_field_must_match_exact_half_open_descriptor_lattice(tmp_path):
    temporal = {
        "kind": "SERIES",
        "start": "2026-08-14T00:00:00Z",
        "end": "2026-08-14T02:00:00Z",
        "cadence_s": "3600",
    }
    value = _field()
    value["time"] = [
        "2026-08-14T00:00:00Z",
        "2026-08-14T01:00:00Z",
    ]
    value["components"]["value"] = [
        [[1.0, 2.0], [3.0, 4.0]],
        [[5.0, 6.0], [7.0, 8.0]],
    ]
    graph = _graph(value, configuration=_configuration(temporal=temporal))
    with WorkflowController(tmp_path) as controller:
        run_id = controller.create_run(graph)
        assert controller.run_until_terminal(run_id) is RunState.SUCCEEDED

    bad = copy.deepcopy(value)
    bad["time"][1] = "2026-08-14T01:30:00Z"
    bad_graph = _graph(bad, configuration=_configuration(temporal=temporal))
    with WorkflowController(tmp_path / "bad") as controller:
        run_id = controller.create_run(bad_graph)
        assert controller.run_until_terminal(run_id) is RunState.FAILED
