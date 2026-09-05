from src.agents.triage import BuildAction, DiagnosticianVerdict, route


def test_no_violation_passes():
    decision = route(DiagnosticianVerdict(matched=False))
    assert decision.action == BuildAction.PASS


def test_low_risk_logs_and_continues():
    decision = route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=3.0))
    assert decision.action == BuildAction.LOG_WARNING


def test_high_risk_freezes():
    decision = route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=8.0))
    assert decision.action == BuildAction.FREEZE


def test_boundary_exactly_at_threshold_freezes():
    # PIIE-001's own threshold (finding #41: lowered to 7.0, see
    # src/taxonomy.py) — not the old uniform 7.5.
    decision = route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=7.0))
    assert decision.action == BuildAction.FREEZE


def test_matched_without_score_raises():
    import pytest
    with pytest.raises(ValueError):
        route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=None))


def test_per_category_thresholds_actually_differ():
    # Finding #41: this is the behavior a single global threshold could
    # never produce - the identical risk_score routes differently
    # depending on taxonomy_id, because the categories are not treated as
    # equally severe. 7.2 is below ALBP-001's default 7.5 threshold (logs,
    # doesn't freeze) but at/above TLGP-002's lowered 7.0 threshold
    # (freezes) - same score, different real-world consequence.
    albp = route(DiagnosticianVerdict(matched=True, taxonomy_id="ALBP-001", risk_score=7.2))
    tlgp002 = route(DiagnosticianVerdict(matched=True, taxonomy_id="TLGP-002", risk_score=7.2))

    assert albp.action == BuildAction.LOG_WARNING
    assert tlgp002.action == BuildAction.FREEZE


def test_unknown_taxonomy_id_falls_back_to_the_conservative_default_instead_of_crashing():
    # Triage is meant to be simple, reliable routing logic - an
    # unrecognized taxonomy_id (which tests/test_taxonomy_consistency.py
    # should prevent from ever reaching here for real) should degrade to
    # the safe fallback threshold, not raise and abort the whole PR's
    # remaining fragments.
    decision = route(DiagnosticianVerdict(matched=True, taxonomy_id="NOT-A-REAL-ID", risk_score=7.5))
    assert decision.action == BuildAction.FREEZE
