import pytest

from src.agents.detector import DetectorOutput
from src.agents.triage import BuildAction, route


def _matched(taxonomy_id: str, risk_score: float) -> DetectorOutput:
    # route() takes the real DetectorOutput directly, whose "matched implies populated"
    # validator requires these fields whenever matched=True, so tests construct a fully valid
    # instance rather than a bare partial one.
    return DetectorOutput(
        matched=True, taxonomy_id=taxonomy_id, risk_score=risk_score,
        plain_english_summary="test summary", citation="test citation", remediation_patch="",
    )


def test_no_violation_passes():
    decision = route(DetectorOutput(matched=False))
    assert decision.action == BuildAction.PASS


def test_low_risk_logs_and_continues():
    decision = route(_matched("PIIE-001", 3.0))
    assert decision.action == BuildAction.LOG_WARNING
    assert not decision.blocks_build
    assert not decision.creates_alert


def test_high_risk_freezes():
    decision = route(_matched("PIIE-001", 8.0))
    assert decision.action == BuildAction.FREEZE
    assert decision.blocks_build
    assert decision.creates_alert


def test_middle_band_creates_an_alert_without_blocking_the_build():
    # The point of the three-band model: a confirmed finding scored below the blocking bar must
    # still reach a human, but must NOT stop the merge.
    decision = route(_matched("PIIE-001", 6.0))
    assert decision.action == BuildAction.REVIEW
    assert not decision.blocks_build
    assert decision.creates_alert


def test_review_band_boundaries_are_inclusive_at_the_bottom_exclusive_at_the_top():
    # PIIE-001: review floor 5.0, blocking bar 7.0.
    assert route(_matched("PIIE-001", 4.9)).action == BuildAction.LOG_WARNING
    assert route(_matched("PIIE-001", 5.0)).action == BuildAction.REVIEW
    assert route(_matched("PIIE-001", 6.9)).action == BuildAction.REVIEW
    assert route(_matched("PIIE-001", 7.0)).action == BuildAction.FREEZE


def test_boundary_exactly_at_threshold_freezes():
    # PIIE-001's own threshold is 7.0 (see src/taxonomy.py), not a uniform 7.5.
    decision = route(_matched("PIIE-001", 7.0))
    assert decision.action == BuildAction.FREEZE


def test_matched_without_score_raises_at_construction():
    # DetectorOutput's validator prevents this invalid state from ever being constructed, a
    # stronger guarantee than route() happening to check for it.
    with pytest.raises(ValueError):
        DetectorOutput(matched=True, taxonomy_id="PIIE-001", risk_score=None)


def test_route_still_defends_against_a_missing_score_directly():
    # Defense in depth: even if an invalid instance reaches route() by
    # bypassing pydantic validation (model_construct, or a future caller
    # that isn't actually a DetectorOutput), route() must not crash
    # several frames away from the real cause.
    invalid = DetectorOutput.model_construct(matched=True, taxonomy_id="PIIE-001", risk_score=None)
    with pytest.raises(ValueError):
        route(invalid)


def test_per_category_thresholds_actually_differ():
    # A single global threshold could never produce this: the identical risk_score routes
    # differently depending on taxonomy_id, because the categories are not equally severe. 7.2 is
    # below ALBP-001's default 7.5 blocking bar but at/above TLGP-002's lowered 7.0 bar, so one
    # stops the merge and one doesn't.
    albp = route(_matched("ALBP-001", 7.2))
    tlgp002 = route(_matched("TLGP-002", 7.2))

    assert albp.action == BuildAction.REVIEW
    assert not albp.blocks_build
    assert tlgp002.action == BuildAction.FREEZE
    assert tlgp002.blocks_build


def test_unknown_taxonomy_id_falls_back_to_the_conservative_default_instead_of_crashing():
    # Triage is meant to be simple, reliable routing logic - an
    # unrecognized taxonomy_id (which tests/test_taxonomy_consistency.py
    # should prevent from ever reaching here for real) should degrade to
    # the safe fallback threshold, not raise and abort the whole PR's
    # remaining fragments.
    decision = route(_matched("NOT-A-REAL-ID", 7.5))
    assert decision.action == BuildAction.FREEZE
