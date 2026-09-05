import pytest

from src.agents.diagnostician import DiagnosticianOutput
from src.agents.triage import BuildAction, route


def _matched(taxonomy_id: str, risk_score: float) -> DiagnosticianOutput:
    # finding #24: route() takes the real DiagnosticianOutput directly now
    # (no more hand-copied DiagnosticianVerdict duplicate) - its own
    # "matched implies populated" validator (finding #46) requires these
    # fields whenever matched=True, so tests construct a fully valid
    # instance rather than a bare partial one.
    return DiagnosticianOutput(
        matched=True, taxonomy_id=taxonomy_id, risk_score=risk_score,
        plain_english_summary="test summary", citation="test citation", remediation_patch="",
    )


def test_no_violation_passes():
    decision = route(DiagnosticianOutput(matched=False))
    assert decision.action == BuildAction.PASS


def test_low_risk_logs_and_continues():
    decision = route(_matched("PIIE-001", 3.0))
    assert decision.action == BuildAction.LOG_WARNING


def test_high_risk_freezes():
    decision = route(_matched("PIIE-001", 8.0))
    assert decision.action == BuildAction.FREEZE


def test_boundary_exactly_at_threshold_freezes():
    # PIIE-001's own threshold (finding #41: lowered to 7.0, see
    # src/taxonomy.py) — not the old uniform 7.5.
    decision = route(_matched("PIIE-001", 7.0))
    assert decision.action == BuildAction.FREEZE


def test_matched_without_score_raises_at_construction():
    # Finding #46's validator now prevents this invalid state from ever
    # being constructed in the first place — a stronger guarantee than the
    # old "route() happens to check for it" behavior.
    with pytest.raises(ValueError):
        DiagnosticianOutput(matched=True, taxonomy_id="PIIE-001", risk_score=None)


def test_route_still_defends_against_a_missing_score_directly():
    # Defense in depth: even if an invalid instance reaches route() by
    # bypassing pydantic validation (model_construct, or a future caller
    # that isn't actually a DiagnosticianOutput), route() must not crash
    # several frames away from the real cause (finding #51).
    invalid = DiagnosticianOutput.model_construct(matched=True, taxonomy_id="PIIE-001", risk_score=None)
    with pytest.raises(ValueError):
        route(invalid)


def test_per_category_thresholds_actually_differ():
    # Finding #41: this is the behavior a single global threshold could
    # never produce - the identical risk_score routes differently
    # depending on taxonomy_id, because the categories are not treated as
    # equally severe. 7.2 is below ALBP-001's default 7.5 threshold (logs,
    # doesn't freeze) but at/above TLGP-002's lowered 7.0 threshold
    # (freezes) - same score, different real-world consequence.
    albp = route(_matched("ALBP-001", 7.2))
    tlgp002 = route(_matched("TLGP-002", 7.2))

    assert albp.action == BuildAction.LOG_WARNING
    assert tlgp002.action == BuildAction.FREEZE


def test_unknown_taxonomy_id_falls_back_to_the_conservative_default_instead_of_crashing():
    # Triage is meant to be simple, reliable routing logic - an
    # unrecognized taxonomy_id (which tests/test_taxonomy_consistency.py
    # should prevent from ever reaching here for real) should degrade to
    # the safe fallback threshold, not raise and abort the whole PR's
    # remaining fragments.
    decision = route(_matched("NOT-A-REAL-ID", 7.5))
    assert decision.action == BuildAction.FREEZE
