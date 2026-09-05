import hashlib
import hmac

import pytest
from fastapi import HTTPException

import src.api.main as main_module


def test_no_secret_configured_fails_closed_by_default(monkeypatch):
    # review finding #1: this used to silently allow any unsigned payload
    # through when the secret wasn't configured — a real production
    # misconfiguration would have let anyone trigger the pipeline. Now it
    # must fail loudly (500, "misconfigured") rather than fail open.
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(main_module, "ALLOW_UNSIGNED", False)
    with pytest.raises(HTTPException) as exc_info:
        main_module._verify_signature(b'{"a": 1}', None)
    assert exc_info.value.status_code == 500


def test_no_secret_configured_allows_through_with_explicit_opt_out(monkeypatch):
    # the fail-open behavior still exists, but only as an explicit,
    # named opt-in for local dev (SHIP_ALLOW_UNSIGNED=true) — not the default.
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "")
    monkeypatch.setattr(main_module, "ALLOW_UNSIGNED", True)
    main_module._verify_signature(b'{"a": 1}', None)  # should not raise


def test_valid_signature_passes(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "test-secret")
    body = b'{"action": "opened"}'
    valid_sig = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    main_module._verify_signature(body, valid_sig)  # should not raise


def test_missing_signature_header_rejected(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "test-secret")
    with pytest.raises(HTTPException) as exc_info:
        main_module._verify_signature(b'{"a": 1}', None)
    assert exc_info.value.status_code == 401


def test_wrong_signature_rejected(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "test-secret")
    body = b'{"action": "opened"}'
    wrong_sig = "sha256=" + hmac.new(b"wrong-secret", body, hashlib.sha256).hexdigest()
    with pytest.raises(HTTPException) as exc_info:
        main_module._verify_signature(body, wrong_sig)
    assert exc_info.value.status_code == 401


def test_tampered_body_rejected(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "test-secret")
    original_body = b'{"action": "opened"}'
    valid_sig = "sha256=" + hmac.new(b"test-secret", original_body, hashlib.sha256).hexdigest()
    tampered_body = b'{"action": "closed"}'
    with pytest.raises(HTTPException):
        main_module._verify_signature(tampered_body, valid_sig)
