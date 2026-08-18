"""Scientific-evidence honesty gate for Stage 2."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]


def test_wind_evidence_pack_is_schema_valid_and_source_inventory_is_exact():
    pack = json.loads(
        (ROOT / "stage2/wind_evidence_pack_v1.json").read_text())
    schema = json.loads(
        (ROOT / "stage2/wind_evidence_pack_v1.schema.json").read_text())
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()).validate(pack)

    inventory = ROOT / pack["source_inventory"]["path"]
    assert hashlib.sha256(inventory.read_bytes()).hexdigest() == (
        pack["source_inventory"]["sha256"])


def test_real_wind_quality_is_not_fabricated():
    pack = json.loads(
        (ROOT / "stage2/wind_evidence_pack_v1.json").read_text())

    assert pack["status"] == "UNAVAILABLE"
    assert pack["decision_policy"]["quality_sensitive_request"] == (
        "CHOICE_REQUIRED")
    assert pack["reference_observations"]["manifest_id"] is None
    assert pack["scope"]["status"] == "UNFROZEN"
    assert all(claim["status"] == "UNKNOWN"
               and claim["estimate"] is None
               and claim["conservative_bound"] is None
               for claim in pack["empirical_claims"])

    era5 = next(item for item in pack["candidate_facts"]
                if item["candidate_id"] == "era5_arco_10m")
    assert era5["facts"]["native_horizontal_scale"]["basis"] == "ANGULAR"
    assert "effective_resolution" not in era5["facts"]
    assert pack["legacy_metadata_policy"]["status"] == "QUARANTINED"


def test_evidence_gap_ledger_covers_the_pinned_inventory_exactly():
    pack = json.loads(
        (ROOT / "stage2/wind_evidence_pack_v1.json").read_text())
    inventory = json.loads(
        (ROOT / "stage0/wind_evidence_inventory_v0.json").read_text())

    assert {item["candidate_id"] for item in pack["candidate_facts"]} == {
        item["id"] for item in inventory["producer_candidates"]
    }
    assert len(pack["candidate_facts"]) == len({
        item["candidate_id"] for item in pack["candidate_facts"]
    })
    assert len(pack["empirical_claims"]) == len({
        item["metric_definition_id"] for item in pack["empirical_claims"]
    }) == len(inventory["metrics"])


# -- the declared policy must bind the running system --------------------


def _pack() -> dict:
    return json.loads((ROOT / "stage2/wind_evidence_pack_v1.json").read_text())


def test_no_empirical_claim_carries_a_number():
    """The pack may not quietly acquire an estimate it never measured.

    `status: UNAVAILABLE` is only meaningful if nothing underneath it reports a
    value. A claim with an estimate or a bound would be fabricated science
    regardless of the status string above it.
    """
    claims = _pack()["empirical_claims"]
    assert claims, "an empty claim list would make this test vacuous"
    for claim in claims:
        assert claim["status"] == "UNKNOWN", claim["metric_definition_id"]
        assert claim["estimate"] is None
        assert claim["conservative_bound"] is None
        assert claim["reference_manifest_id"] is None
        assert claim["reason_code"]


def test_the_declared_decision_policy_binds_the_running_system():
    """The pack declares CHOICE_REQUIRED; Stage 6 must actually do it.

    Without this, the policy is a string in a JSON file. Someone could relax
    the resolver into auto-selecting on quality, or edit this policy, and
    nothing would disagree -- which is precisely how conformance evidence gets
    relabelled as science.
    """
    from objectives import ObjectiveStatus
    from tests.test_stage6_integration import quality_request
    import stage6.fixtures as fx

    policy = _pack()["decision_policy"]
    assert policy["quality_sensitive_request"] == "CHOICE_REQUIRED"

    outcome = quality_request(fx.make_stage6_fixture())
    assert outcome.status.value == policy["quality_sensitive_request"]
    assert outcome.status is ObjectiveStatus.CHOICE_REQUIRED
    # The system did not choose, and did not claim it could rank.
    assert outcome.resolution is None
    assert outcome.report.ranking_complete is False
    assert outcome.report.nondominance_claimed is False


def test_cost_selection_is_gated_behind_hard_constraints_as_declared():
    policy = _pack()["decision_policy"]
    assert policy["cost_selection"] == (
        "ONLY_AFTER_HARD_COMPATIBILITY_CONSTRAINTS_PASS")


def test_the_scope_is_unfrozen_so_no_result_may_be_generalised():
    """An unfrozen scope means no AOI/window/regime was ever reviewed."""
    pack = _pack()
    assert pack["status"] == "UNAVAILABLE"
    assert pack["scope"]["status"] == "UNFROZEN"
    assert pack["reference_observations"]["status"] == "UNAVAILABLE"
    assert pack["reference_observations"]["manifest_id"] is None
    assert pack["reference_observations"]["reason_code"] == (
        "NO_REVIEWED_IMMUTABLE_HELD_OUT_REFERENCE")
