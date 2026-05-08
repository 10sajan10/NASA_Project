"""CheckpointStore tests + a producer-shaped resume scenario."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import CheckpointStore


def test_save_load_round_trip(tmp_path):
    store = CheckpointStore(tmp_path / "ckpt.duckdb")
    state = {"step": 7, "Q": np.full((4, 4), 3.14, dtype="float32")}
    store.save("kbdi", step_id=10, state=state)
    loaded = store.load_step("kbdi", 10)
    assert loaded["step"] == 7
    assert np.array_equal(loaded["Q"], state["Q"])


def test_load_latest_picks_highest_step(tmp_path):
    store = CheckpointStore(tmp_path / "ckpt.duckdb")
    store.save("p", 1, {"v": 1})
    store.save("p", 5, {"v": 5})
    store.save("p", 3, {"v": 3})
    step_id, state = store.load_latest("p")
    assert step_id == 5
    assert state == {"v": 5}


def test_load_latest_returns_none_when_empty(tmp_path):
    store = CheckpointStore(tmp_path / "ckpt.duckdb")
    assert store.load_latest("p") is None
    assert store.load_step("p", 0) is None
    assert store.list_steps("p") == []


def test_overwrite_same_step(tmp_path):
    store = CheckpointStore(tmp_path / "ckpt.duckdb")
    store.save("p", 0, {"x": 1})
    store.save("p", 0, {"x": 99})
    assert store.load_step("p", 0) == {"x": 99}
    assert store.list_steps("p") == [0]


def test_delete_wipes_all_checkpoints_for_producer(tmp_path):
    store = CheckpointStore(tmp_path / "ckpt.duckdb")
    for i in range(5):
        store.save("p", i, {"step": i})
    store.save("other", 0, {"keep": True})
    n = store.delete("p")
    assert n == 5
    assert store.load_latest("p") is None
    # Other producer untouched
    assert store.load_latest("other")[1] == {"keep": True}


def test_resume_pattern(tmp_path):
    """Producer-shaped scenario: integrate forward, crash midway, resume,
    finish. Verifies the recommended pattern in the module docstring."""
    db = tmp_path / "ckpt.duckdb"

    def integrate(start: int, n_steps: int, *, crash_at: int | None = None,
                  store: CheckpointStore, producer: str = "kbdi"):
        last = store.load_latest(producer)
        step = (last[0] + 1) if last else start
        state = last[1] if last else {"value": 0}
        while step < start + n_steps:
            state["value"] += 1
            store.save(producer, step, state)
            if crash_at is not None and step == crash_at:
                raise RuntimeError("simulated crash")
            step += 1
        return state["value"]

    store = CheckpointStore(db)
    # First run crashes after 3 steps.
    with pytest.raises(RuntimeError):
        integrate(0, 10, crash_at=3, store=store)

    # Resume picks up from step 4.
    final = integrate(0, 10, store=store)
    assert final == 10
    # All 10 steps saved.
    assert store.list_steps("kbdi") == list(range(10))


def test_concurrent_processes_share_store(tmp_path):
    """Two short-lived connections to the same DB file don't corrupt each
    other. DuckDB's OS-level lock arbitrates writes."""
    db = tmp_path / "ckpt.duckdb"
    store_a = CheckpointStore(db)
    store_b = CheckpointStore(db)
    store_a.save("p", 1, {"who": "a"})
    store_b.save("p", 2, {"who": "b"})
    assert store_a.load_step("p", 2) == {"who": "b"}
    assert store_b.load_step("p", 1) == {"who": "a"}
