"""
Finding #62: fetch_pr_diff()'s raise_for_status() was uncaught in
github_webhook()'s "fast, reliable" leg - a closed/deleted PR, an expired
token, or a transient GitHub 5xx turned into an unhandled requests.HTTPError
and a bare 500 with no logged context, in the exact part of the handler
meant to be fast and reliable, before the async fix (finding #44) even
gets a chance to run.
"""

import hashlib
import hmac

import pytest
import requests
from fastapi.testclient import TestClient

import src.api.main as main_module
from src.api.main import app

REAL_PR_EVENT = {
    "action": "opened",
    "number": 1,
    "pull_request": {"number": 1, "title": "Add AI-assisted underwriting opinion"},
    "repository": {"full_name": "pandayv/micro-finance"},
}


def _signed_post(client, payload_bytes, secret):
    sig = "sha256=" + hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return client.post(
        "/api/v1/webhook",
        content=payload_bytes,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(main_module, "ALLOWED_REPOS", {"pandayv/micro-finance"})
    return TestClient(app)


def test_upstream_github_error_returns_a_clean_502_not_an_unhandled_500(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.exceptions.HTTPError("404 Client Error: Not Found for url: ...")

    monkeypatch.setattr(main_module, "fetch_pr_diff", _raise)

    import json

    body = json.dumps(REAL_PR_EVENT).encode()
    response = _signed_post(client, body, "test-secret")

    assert response.status_code == 502
    assert "Could not fetch PR diff" in response.json()["detail"]


def test_transient_connection_error_also_returns_a_clean_502(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(main_module, "fetch_pr_diff", _raise)

    import json

    body = json.dumps(REAL_PR_EVENT).encode()
    response = _signed_post(client, body, "test-secret")

    assert response.status_code == 502
