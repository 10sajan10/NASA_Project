"""Stage-6 named selection policies, comparability, and the choice path.

The claims under test are that a quality request never auto-selects, that two
metrics are only called comparable when the same reference and evaluator make
them so, and that a human's choice is checked against the exact report it was
made from.
"""
from __future__ import annotations

import pytest

from contracts import BoundKind, EvidenceStatus, UncertaintyStatus
from objectives import (
    ChoiceRecord,
    ChoiceRequiredReport,
    ComparabilityCode,
    ComparabilityVerdict,
    FallbackDecision,
    MetricReading,
    ObjectiveRequest,
    SelectionObjective,
    SourceAlternative,
    assess_comparability,
)
from plans import ProducerKind
from resolution import ProducerSelectionRef

METRIC = "example.metric.error"


def _reading(producer_id: str, value: str | None = "0.5", **overrides):
    base = dict(
        producer_id=producer_id, metric_definition_id=METRIC,
        status=EvidenceStatus.KNOWN if value is not None
        else EvidenceStatus.UNKNOWN,
        unit="m.s-1", value=value, bound_kind=BoundKind.POINT_ESTIMATE,
        reference_manifest_id="ref-1", evaluator_id="eval-1",
        protocol_id="protocol-1", applicability_covers_request=True,
        uncertainty_status=UncertaintyStatus.KNOWN,
        uncertainty_lower="0.4", uncertainty_upper="0.6",
        uncertainty_confidence_level="0.95",
        uncertainty_method_id="block-bootstrap")
    base.update(overrides)
    return MetricReading(**base)


# -- comparability --------------------------------------------------------


def test_same_reference_and_evaluator_are_comparable():
    verdict = assess_comparability(
        (_reading("a", "0.5", uncertainty_lower="0.4", uncertainty_upper="0.6"),
         _reading("b", "0.9", uncertainty_lower="0.8", uncertainty_upper="1.0")),
        METRIC)
    assert verdict.comparable
    assert verdict.blocking_codes == ()
    assert verdict.intervals_overlap is False
    assert verdict.separation_established


def test_overlapping_intervals_do_not_establish_separation():
    verdict = assess_comparability(
        (_reading("a", "0.5", uncertainty_lower="0.3", uncertainty_upper="0.7"),
         _reading("b", "0.6", uncertainty_lower="0.4", uncertainty_upper="0.8")),
        METRIC)
    assert verdict.comparable
    assert verdict.intervals_overlap is True
    # Comparable but not separated: a smaller point estimate is not a winner.
    assert not verdict.separation_established


def test_missing_intervals_are_treated_as_unresolved():
    verdict = assess_comparability(
        (_reading("a", uncertainty_status=UncertaintyStatus.UNKNOWN,
                  uncertainty_lower=None, uncertainty_upper=None,
                  uncertainty_confidence_level=None,
                  uncertainty_method_id=None),
         _reading("b", "0.9")),
        METRIC)
    assert verdict.comparable
    assert verdict.intervals_overlap is True
    assert not verdict.separation_established


@pytest.mark.parametrize("override,code", [
    ({"reference_manifest_id": "ref-2"},
     ComparabilityCode.REFERENCE_MANIFEST_DIFFERS),
    ({"evaluator_id": "eval-2"}, ComparabilityCode.EVALUATOR_DIFFERS),
    ({"protocol_id": "protocol-2"}, ComparabilityCode.PROTOCOL_DIFFERS),
    ({"unit": "km.h-1"}, ComparabilityCode.UNIT_DIFFERS),
    ({"bound_kind": BoundKind.CONSERVATIVE_UPPER},
     ComparabilityCode.BOUND_KIND_DIFFERS),
    ({"applicability_covers_request": False},
     ComparabilityCode.APPLICABILITY_DOES_NOT_COVER_REQUEST),
])
def test_provenance_differences_block_comparison(override, code):
    verdict = assess_comparability(
        (_reading("a"), _reading("b", **override)), METRIC)
    assert not verdict.comparable
    assert code in verdict.blocking_codes
    assert not verdict.separation_established
    assert verdict.detail


def test_unknown_metric_blocks_comparison():
    verdict = assess_comparability(
        (_reading("a"), _reading("b", value=None)), METRIC)
    assert not verdict.comparable
    assert ComparabilityCode.METRIC_NOT_KNOWN in verdict.blocking_codes


def test_a_single_alternative_is_not_a_comparison():
    verdict = assess_comparability((_reading("a"),), METRIC)
    assert not verdict.comparable
    assert verdict.blocking_codes == (ComparabilityCode.SINGLE_ALTERNATIVE,)


def test_all_blocking_reasons_are_reported_together():
    verdict = assess_comparability(
        (_reading("a"),
         _reading("b", reference_manifest_id="ref-2", evaluator_id="eval-2")),
        METRIC)
    assert {ComparabilityCode.REFERENCE_MANIFEST_DIFFERS,
            ComparabilityCode.EVALUATOR_DIFFERS}.issubset(
                set(verdict.blocking_codes))


def test_comparability_verdict_round_trips():
    verdict = assess_comparability((_reading("a"), _reading("b", "0.9")), METRIC)
    assert ComparabilityVerdict.from_dict(verdict.to_dict()) == verdict


def test_a_verdict_cannot_disagree_with_its_codes():
    with pytest.raises(ValueError, match="must agree"):
        ComparabilityVerdict(True, METRIC,
                             (ComparabilityCode.UNIT_DIFFERS,), "x",
                             intervals_overlap=False)


# -- policy ---------------------------------------------------------------


def _producer(name: str = "inv-1") -> ProducerSelectionRef:
    return ProducerSelectionRef(ProducerKind.INVOCATION, name)


def _choice(report_id: str = "a" * 64) -> ChoiceRecord:
    return ChoiceRecord(report_id, _producer(), "v1", "because")


def test_minimum_cost_requests_carry_no_decision():
    request = ObjectiveRequest()
    assert request.objective is SelectionObjective.MINIMUM_COST
    assert request.decision is None and request.decision_id is None
    with pytest.raises(ValueError, match="never blocked on one"):
        ObjectiveRequest(SelectionObjective.MINIMUM_COST, choice=_choice())


def test_a_request_cannot_carry_both_a_choice_and_a_fallback():
    fallback = FallbackDecision("a" * 64, SelectionObjective.MINIMUM_COST, "why")
    with pytest.raises(ValueError, match="never both"):
        ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY,
                         choice=_choice(), fallback=fallback)


def test_quality_cannot_be_its_own_fallback():
    with pytest.raises(ValueError, match="cannot be its own fallback"):
        FallbackDecision("a" * 64, SelectionObjective.EMPIRICAL_QUALITY, "why")


def test_a_choice_is_bound_to_the_report_it_was_made_from():
    first = _choice("a" * 64)
    second = _choice("b" * 64)
    # The same producer chosen against a different report is a different
    # decision, so the identity must differ.
    assert first.decision_id != second.decision_id


def test_decisions_round_trip():
    choice = _choice()
    assert ChoiceRecord.from_dict(choice.to_dict()) == choice
    fallback = FallbackDecision("a" * 64, SelectionObjective.MINIMUM_COST, "why")
    assert FallbackDecision.from_dict(fallback.to_dict()) == fallback
    request = ObjectiveRequest(SelectionObjective.EMPIRICAL_QUALITY,
                               choice=choice)
    assert ObjectiveRequest.from_dict(request.to_dict()) == request


# -- report ---------------------------------------------------------------


def _alternative(name: str, cost: int, admissible: bool = True):
    return SourceAlternative(
        producer=_producer(name), capability_id=name, admissible=admissible,
        plan_id=("p" * 64) if admissible else None,
        cost_units=cost if admissible else None,
        resolution_status="READY" if admissible else "UNSATISFIABLE",
        reading=_reading(name),
        detail="" if admissible else "no globally consistent plan")


def test_a_report_never_claims_a_ranking_or_nondominance():
    report = ChoiceRequiredReport.bind(
        concept_id="c", metric_definition_id=METRIC,
        alternatives=(_alternative("a", 5), _alternative("b", 8)),
        comparability=assess_comparability(
            (_reading("a"), _reading("b", "0.9")), METRIC))
    assert report.ranking_complete is False
    assert report.nondominance_claimed is False
    assert report.report_id == report.expected_id()
    assert len(report.admissible) == 2


def test_a_report_round_trips_and_rejects_forged_ranking_claims():
    report = ChoiceRequiredReport.bind(
        concept_id="c", metric_definition_id=METRIC,
        alternatives=(_alternative("a", 5), _alternative("b", 8)),
        comparability=assess_comparability(
            (_reading("a"), _reading("b", "0.9")), METRIC))
    assert ChoiceRequiredReport.from_dict(report.to_dict()) == report

    forged = report.to_dict()
    forged["ranking_complete"] = True
    with pytest.raises(ValueError, match="cannot claim a complete ranking"):
        ChoiceRequiredReport.from_dict(forged)


def test_an_inadmissible_alternative_must_explain_itself():
    with pytest.raises(ValueError, match="must explain itself"):
        SourceAlternative(
            producer=_producer(), capability_id="x", admissible=False,
            plan_id=None, cost_units=None, resolution_status="UNSATISFIABLE",
            reading=None, detail="")
