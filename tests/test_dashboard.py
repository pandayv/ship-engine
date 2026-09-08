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
        status="frozen", created_at="2026-09-04T00:00:00+00:00", head_sha="abc123sha",
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


def _wire_resolvable(monkeypatch, calls, alert=None):
    """Stubs everything a decision touches: the lookup, the state change,
    and both GitHub write-back calls (which must never reach the network
    from a test)."""
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(dashboard_module.alert_store, "get_alert", lambda alert_id: alert or _sample_alert(alert_id))
    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert",
                        lambda alert_id, approved: calls.append((alert_id, approved)))
    posted, synced = [], []
    monkeypatch.setattr(dashboard_module, "post_pr_comment", lambda *a, **k: posted.append((a, k)))
    monkeypatch.setattr(dashboard_module, "sync_pr_check", lambda *a, **k: synced.append((a, k)))
    return posted, synced


def test_approve_calls_resolve_with_true(monkeypatch):
    calls = []
    _wire_resolvable(monkeypatch, calls)
    response = client.post(f"/dashboard/a1/approve?token={TEST_TOKEN}",
                           data={"reason": "Fixture data only."}, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("a1", True)]


def test_reject_calls_resolve_with_false(monkeypatch):
    calls = []
    _wire_resolvable(monkeypatch, calls)
    response = client.post(f"/dashboard/a1/reject?token={TEST_TOKEN}",
                           data={"reason": "Real problem, needs an allowlist."}, follow_redirects=False)
    assert response.status_code == 303
    assert calls == [("a1", False)]


def test_decision_writes_back_to_the_pull_request(monkeypatch):
    # The loop only closes if the human's decision reaches GitHub: a
    # comment recording it, and a recomputed commit status. Without both,
    # the merge stays blocked no matter what the reviewer decided here.
    calls = []
    posted, synced = _wire_resolvable(monkeypatch, calls)
    client.post(f"/dashboard/a1/approve?token={TEST_TOKEN}",
                data={"reason": "Fixture data only, tracked as LOAN-812."}, follow_redirects=False)

    assert len(posted) == 1
    repo, pr_number, body = posted[0][0]
    assert repo == "pandayv/micro-finance" and pr_number == 1
    assert "Risk accepted" in body
    assert "Fixture data only, tracked as LOAN-812." in body

    assert synced == [(("pandayv/micro-finance", 1, "abc123sha"), {})]


def test_decision_without_a_reason_is_refused(monkeypatch):
    # The reason IS the audit record — a decision with no stated basis
    # must not resolve the alert at all.
    calls = []
    _wire_resolvable(monkeypatch, calls)
    response = client.post(f"/dashboard/a1/approve?token={TEST_TOKEN}",
                           data={"reason": "   "}, follow_redirects=False)
    assert response.status_code == 400
    assert calls == []


def test_approve_conflict_returns_409(monkeypatch):
    # review finding #3/#4: resolving a bogus or already-resolved alert_id
    # used to either silently create a ghost row or silently overwrite a
    # prior decision — now it's a real 409, not a silent success.
    calls = []
    _wire_resolvable(monkeypatch, calls)

    def _raise(alert_id, approved):
        raise dashboard_module.alert_store.AlertNotFoundOrAlreadyResolved(alert_id)

    monkeypatch.setattr(dashboard_module.alert_store, "resolve_alert", _raise)
    response = client.post(f"/dashboard/nonexistent/approve?token={TEST_TOKEN}",
                           data={"reason": "already handled"}, follow_redirects=False)
    assert response.status_code == 409


def test_unknown_alert_returns_404(monkeypatch):
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(dashboard_module.alert_store, "get_alert", lambda alert_id: None)
    response = client.post(f"/dashboard/ghost/approve?token={TEST_TOKEN}",
                           data={"reason": "n/a"}, follow_redirects=False)
    assert response.status_code == 404


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
    response = client.post("/dashboard/a1/approve", data={"reason": "x"}, follow_redirects=False)
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


def test_dashboard_auth_env_var_is_independent_of_the_webhook_signature_flag():
    # Real bug an independent review caught (2026-09-05): the dashboard's
    # opt-out used to read the exact same SHIP_ALLOW_UNSIGNED env var the
    # webhook uses to bypass its own, unrelated HMAC check — setting one
    # for local webhook testing silently disabled dashboard auth too, with
    # no indication that happened as a side effect. Asserted directly
    # against the source (not just current in-memory values, which could
    # coincidentally differ even if both read the same env var) that the
    # dashboard reads its own dedicated env var and does not reference the
    # webhook's at all.
    import inspect

    dashboard_source = inspect.getsource(dashboard_module)
    assert 'os.environ.get("SHIP_ALLOW_UNAUTHENTICATED_DASHBOARD"' in dashboard_source
    # Check the actual env-var-read call pattern, not just any mention of
    # the string anywhere in the file (this test's own docstring above
    # names it by name, which a naive substring check would also match).
    assert 'os.environ.get("SHIP_ALLOW_UNSIGNED"' not in dashboard_source


def test_wrong_token_uses_constant_time_comparison(monkeypatch):
    # Independent review (2026-09-05): the token check used a plain `!=`
    # rather than hmac.compare_digest, unlike the sibling webhook HMAC
    # check in the same fix pass. This test can't observe timing directly,
    # but confirms the code path actually calls compare_digest rather than
    # `!=` by checking a wrong-but-same-length token is still rejected
    # (compare_digest and `!=` agree on correctness; the real fix is in
    # the source, asserted via inspection below).
    import inspect

    source = inspect.getsource(dashboard_module._require_token)
    assert "hmac.compare_digest" in source
    monkeypatch.setattr(dashboard_module, "DASHBOARD_TOKEN", TEST_TOKEN)
    monkeypatch.setattr(dashboard_module.alert_store, "list_active_alerts", lambda: [])
    response = client.get("/dashboard", params={"token": "x" * len(TEST_TOKEN)})
    assert response.status_code == 401
