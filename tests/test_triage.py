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
    decision = route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=7.5))
    assert decision.action == BuildAction.FREEZE


def test_matched_without_score_raises():
    import pytest
    with pytest.raises(ValueError):
        route(DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=None))
