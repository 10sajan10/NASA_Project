"""Stage-5 durable planning sessions: cursors, quota, cooldowns, freezing.

The invariants under test are that a restarted controller resumes where it
stopped without re-spending quota, that planning and payload transfer draw on
one shared provider budget, and that a frozen availability snapshot cannot be
quietly replaced.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from acquisition import (
    AssetCandidate,
    AssetExtent,
    FrozenSessionError,
    PlanningSessionStore,
    ProviderQuota,
    QuotaExceededError,
)
from contracts import BBoxSupport, SampleSemantics, TemporalKind, TemporalSupport

CRS = "EPSG:4326"
AXES = ("x", "y")


def _candidate(asset_id: str) -> AssetCandidate:
    return AssetCandidate(
        asset_id=asset_id,
        locator=f"https://example.invalid/{asset_id}",
        extent=AssetExtent(
            BBoxSupport(CRS, AXES, ("0", "0", "4", "4")),
            TemporalSupport(
                TemporalKind.SERIES, start="2026-01-01T00:00:00Z",
                end="2026-01-01T02:00:00Z", cadence_s="3600", max_gap_s="0",
                sample_semantics=SampleSemantics.INSTANTANEOUS)),
        byte_size=10,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "planning.sqlite3"


def test_cursor_and_candidates_survive_a_restart(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.record_page("s1", "q1", {"q": 1}, cursor="page-2", exhausted=False,
                      candidates=(_candidate("a"), _candidate("b")))

    # A brand new store object over the same file is a fresh process for all
    # practical purposes: nothing is carried in memory.
    reopened = PlanningSessionStore(db_path)
    state = reopened.cursor_for("s1", "q1")
    assert state.cursor == "page-2"
    assert state.pages_read == 1
    assert state.exhausted is False
    assert [item.asset_id for item in reopened.candidates_for("s1", "q1")] \
        == ["a", "b"]


def test_resuming_appends_in_discovery_order_without_duplicates(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.record_page("s1", "q1", {"q": 1}, cursor="1", exhausted=False,
                      candidates=(_candidate("a"),))
    stored = store.record_page(
        "s1", "q1", {"q": 1}, cursor=None, exhausted=True,
        # 'a' repeats across a page boundary, as a real provider may do.
        candidates=(_candidate("a"), _candidate("b")))
    assert stored == 1
    assert [item.asset_id for item in store.candidates_for("s1", "q1")] \
        == ["a", "b"]
    assert store.cursor_for("s1", "q1").pages_read == 2
    assert store.cursor_for("s1", "q1").exhausted is True


def test_repeated_asset_id_cannot_change_metadata_across_pages(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    candidate = _candidate("a")
    store.record_page("s1", "q1", {"q": 1}, cursor="1", exhausted=False,
                      candidates=(candidate,))
    with pytest.raises(ValueError, match="conflicting metadata"):
        store.record_page(
            "s1", "q1", {"q": 1}, cursor=None, exhausted=True,
            candidates=(dataclasses.replace(
                candidate, locator="https://example.invalid/relabelled"),))
    # The page and cursor advance share a transaction, so the refusal leaves
    # the original durable state intact.
    assert store.cursor_for("s1", "q1").cursor == "1"
    assert store.cursor_for("s1", "q1").pages_read == 1


def test_quota_is_shared_between_planning_and_transfer(db_path):
    store = PlanningSessionStore(db_path)
    quota = ProviderQuota(max_metadata_calls=2, max_payload_calls=2,
                          max_bytes=100)
    store.debit_quota("src", quota, metadata_calls=1)
    store.debit_quota("src", quota, payload_calls=1, transferred_bytes=40)
    usage = store.quota_for("src")
    assert usage.metadata_calls == 1
    assert usage.payload_calls == 1
    assert usage.bytes_transferred == 40

    with pytest.raises(QuotaExceededError) as excinfo:
        store.debit_quota("src", quota, transferred_bytes=61)
    assert excinfo.value.dimension == "byte"
    # A refused debit must not have been applied.
    assert store.quota_for("src").bytes_transferred == 40


def test_quota_survives_restart_so_resuming_cannot_re_spend(db_path):
    quota = ProviderQuota(max_metadata_calls=3)
    PlanningSessionStore(db_path).debit_quota("src", quota, metadata_calls=2)
    reopened = PlanningSessionStore(db_path)
    assert reopened.quota_for("src").metadata_calls == 2
    reopened.debit_quota("src", quota, metadata_calls=1)
    with pytest.raises(QuotaExceededError):
        reopened.debit_quota("src", quota, metadata_calls=1)


def test_quota_is_keyed_by_provider_not_by_session(db_path):
    store = PlanningSessionStore(db_path)
    quota = ProviderQuota(max_metadata_calls=2)
    store.open_session("s1")
    store.open_session("s2")
    store.debit_quota("src", quota, metadata_calls=1)
    store.debit_quota("src", quota, metadata_calls=1)
    with pytest.raises(QuotaExceededError):
        store.debit_quota("src", quota, metadata_calls=1)


def test_cooldowns_persist_and_expire(db_path):
    store = PlanningSessionStore(db_path)
    store.set_cooldown("src", 1_000.0, "provider returned 503")
    assert store.in_cooldown("src", now=999.0)
    assert not store.in_cooldown("src", now=1_001.0)
    reopened = PlanningSessionStore(db_path)
    until, reason = reopened.cooldown_for("src")
    assert until == 1_000.0
    assert reason == "provider returned 503"


def test_recorded_limits_persist_across_restart(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.record_limit("s1", "MAX_PAGES_PER_QUERY", "q1")
    store.record_limit("s1", "MAX_PAGES_PER_QUERY", "q1")  # idempotent
    store.record_limit("s1", "PROVIDER_COOLDOWN", "src")
    assert PlanningSessionStore(db_path).limits_for("s1") == (
        ("MAX_PAGES_PER_QUERY", "q1"), ("PROVIDER_COOLDOWN", "src"))


def test_freezing_is_idempotent_but_refuses_a_different_snapshot(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    assert not store.is_frozen("s1")
    store.freeze_session("s1", "a" * 64)
    store.freeze_session("s1", "a" * 64)
    assert store.is_frozen("s1")
    assert store.snapshot_id_for("s1") == "a" * 64
    with pytest.raises(ValueError, match="already frozen"):
        store.freeze_session("s1", "b" * 64)
    with pytest.raises(KeyError):
        store.freeze_session("unknown", "a" * 64)


def test_frozen_session_is_structurally_read_only(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.freeze_session("s1", "a" * 64)
    with pytest.raises(FrozenSessionError, match="frozen and read-only"):
        store.record_page(
            "s1", "q1", {"q": 1}, cursor=None, exhausted=True,
            candidates=(_candidate("a"),))
    with pytest.raises(FrozenSessionError, match="frozen and read-only"):
        store.record_limit("s1", "MAX_PAGES_PER_QUERY", "q1")
    assert store.known_query_ids("s1") == ()
    assert store.limits_for("s1") == ()


def test_a_persisted_query_cannot_be_relabelled_on_resume(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.record_page("s1", "q1", {"q": 1}, cursor="1", exhausted=False,
                      candidates=(_candidate("a"),))
    with pytest.raises(ValueError, match="cannot be relabelled"):
        store.record_page(
            "s1", "q1", {"q": 2}, cursor=None, exhausted=True,
            candidates=(_candidate("b"),))


def test_open_session_does_not_reset_existing_durable_state(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    store.record_page("s1", "q1", {"q": 1}, cursor="7", exhausted=False,
                      candidates=(_candidate("a"),))
    store.open_session("s1")
    assert store.cursor_for("s1", "q1").cursor == "7"
    assert len(store.candidates_for("s1", "q1")) == 1


def test_session_digest_changes_only_with_durable_state(db_path):
    store = PlanningSessionStore(db_path)
    store.open_session("s1")
    first = store.session_digest("s1")
    assert store.session_digest("s1") == first
    store.record_page("s1", "q1", {"q": 1}, cursor=None, exhausted=True,
                      candidates=(_candidate("a"),))
    assert store.session_digest("s1") != first


def test_relative_database_path_is_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        PlanningSessionStore(Path("planning.sqlite3"))
