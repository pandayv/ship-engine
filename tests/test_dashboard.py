from fastapi.testclient import TestClient

import src.api.dashboard as dashboard_module
from src.api.main import app
from src.storage.alert_store import Alert

client = TestClient(app)

TEST_TOKEN = "test-dashboard-token"


def _sample_alert(alert_id="a1"):
    return Alert(
        alert_id=alert_id, repo="pandayv/micro-finance", pr_number=1, file="loans/ai_underwriting.py",
        taxonomy_id="PIIE-001", risk_score=9.1, plain_english_summary="Raw applicant PII sent to an external LLM.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# redact PII before building the prompt",
        status="frozen", created_at="2026-09-04T00:00:00+00:00",
    )


def test_empty_state(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [])
    response = client.get(f"/dashboard?token={TEST_TOKEN}")
    assert response.status_code == 200
    assert "No active alerts" in response.text


def test_renders_alert_details(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [_sample_alert()])
    response = client.get(f"/dashboard?token={TEST_TOKEN}")
    assert response.status_code == 200
    assert "PIIE-001" in response.text
    assert "GDPR Art. 32(1)(a)" in response.text
    assert "redact PII" in response.text
    assert "loans/ai_underwriting.py" in response.text  # finding #60: file traceability


def test_approve_calls_resolve_with_true(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    calls = []
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", lambda alert_id, approved: calls.append((alert_id, approved)))
    response = client.post(f"/dashboard/a1/approve?token={TEST_TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("a1", True)]


def test_reject_calls_resolve_with_false(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    calls = []
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", lambda alert_id, approved: calls.append((alert_id, approved)))
    response = client.post(f"/dashboard/a1/reject?token={TEST_TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("a1", False)]


def test_approve_conflict_returns_409(monkeypatch):
    # review finding #3/#4: resolving a bogus or already-resolved alert_id
    # used to either silently create a ghost row or silently overwrite a
    # prior decision — now it's a real 409, not a silent success.
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)

    def _raise(alert_id, approved):
        raise dashboard_module.alert_store.AlertNotFoundOrAlreadyResolved(alert_id)

    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", _raise)
    response = client.post(f"/dashboard/nonexistent/approve?token={TEST_TOKEN}", follow_redirects=False)
    assert response.status_code == 409


# --- Auth-specific tests (review finding #2: no auth at all previously) ---


def test_missing_token_rejected(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    response = client.get("/dashboard")
    assert response.status_code == 401


def test_wrong_token_rejected(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    response = client.get("/dashboard?token=wrong-token")
    assert response.status_code == 401


def test_approve_without_token_rejected(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    calls = []
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", lambda alert_id, approved: calls.append((alert_id, approved)))
    response = client.post("/dashboard/a1/approve", follow_redirects=False)
    assert response.status_code == 401
    assert calls == []  # the actual resolution must never have been attempted


def test_no_token_configured_fails_closed_by_default(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", "")
    monkeypatch.setattr(dashboard_module, "ALLOW_UNAUTHENTICATED", False)
    response = client.get("/dashboard")
    assert response.status_code == 500


def test_no_token_configured_allows_through_with_explicit_opt_out(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", "")
    monkeypatch.setattr(dashboard_module, "ALLOW_UNAUTHENTICATED", True)
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [])
    response = client.get("/dashboard")
    assert response.status_code == 200
