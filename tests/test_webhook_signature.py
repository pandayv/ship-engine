import hashlib
import hmac

import pytest
from fastapi import HTTPException

import src.api.main as main_module


def test_no_secret_configured_allows_through(monkeypatch):
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "")
    # should not raise, even with no signature header, when secret isn't provisioned
    main_module._verify_signature(b'{"a": 1}', None)


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
