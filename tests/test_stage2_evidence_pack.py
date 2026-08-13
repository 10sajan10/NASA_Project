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
