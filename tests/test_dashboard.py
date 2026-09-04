from fastapi.testclient import TestClient

import src.api.dashboard as dashboard_module
from src.api.main import app
from src.storage.alert_store import Alert

client = TestClient(app)


def _sample_alert(alert_id="a1"):
    return Alert(
        alert_id=alert_id, repo="pandayv/micro-finance", pr_number=1, taxonomy_id="PIIE-001",
        risk_score=9.1, plain_english_summary="Raw applicant PII sent to an external LLM.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# redact PII before building the prompt",
        status="frozen", created_at="2026-09-04T00:00:00+00:00",
    )


def test_empty_state(monkeypatch):
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [])
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "No active alerts" in response.text


def test_renders_alert_details(monkeypatch):
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [_sample_alert()])
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "PIIE-001" in response.text
    assert "GDPR Art. 32(1)(a)" in response.text
    assert "redact PII" in response.text


def test_approve_calls_resolve_with_true(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", lambda alert_id, approved: calls.append((alert_id, approved)))
    response = client.post("/dashboard/a1/approve", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert calls == [("a1", True)]


def test_reject_calls_resolve_with_false(monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", lambda alert_id, approved: calls.append((alert_id, approved)))
    response = client.post("/dashboard/a1/reject", follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("a1", False)]
