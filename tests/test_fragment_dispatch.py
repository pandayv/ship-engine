"""
Tests for the 2026-09-05 parallel per-fragment dispatch redesign (see
src/api/main.py's module docstring for the full architecture change).

_dispatch_fragments() replaces the old _invoke_async_processing(), which
self-invoked the same Lambda with the whole diff and processed every
escalated fragment sequentially inside one Lambda invocation, bounded by
Lambda's hard 900-second timeout. The new version splits the diff, scans
each fragment, and sends one SQS message per escalated fragment instead -
each fragment becomes its own independent, concurrently-processable job.
"""

import json
from unittest.mock import MagicMock, patch

import src.api.main as main_module
from src.api.main import _dispatch_fragments

CODE_DIFF = '''diff --git a/loans/apply.py b/loans/apply.py
--- a/loans/apply.py
+++ b/loans/apply.py
@@ -1,3 +1,7 @@
+def process_application(applicant):
+    logger.info(f"SSN: {applicant.ssn}")
+    return True
diff --git a/loans/clean.py b/loans/clean.py
--- a/loans/clean.py
+++ b/loans/clean.py
@@ -1,3 +1,4 @@
+def calculate_payment(principal, rate):
+    return principal * rate
'''


def test_sends_one_sqs_message_per_escalated_fragment_only(monkeypatch):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client) as mock_boto_client:
        result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF)

    mock_boto_client.assert_called_once_with("sqs")
    # Only loans/apply.py should escalate (SSN in a logging call);
    # loans/clean.py has nothing Screener flags.
    assert mock_client.send_message.call_count == 1
    _, kwargs = mock_client.send_message.call_args
    assert kwargs["QueueUrl"] == "https://sqs.us-west-2.amazonaws.com/123/fragment-queue"
    body = json.loads(kwargs["MessageBody"])
    assert body["repo_full_name"] == "pandayv/micro-finance"
    assert body["pr_number"] == 1
    assert body["file"] == "loans/apply.py"
    assert "ssn" in body["isolated_fragment"].lower()
    assert result["action"] == "accepted"


def test_falls_back_to_process_pr_when_no_queue_configured(monkeypatch):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "")
    calls = []
    monkeypatch.setattr(main_module, "process_pr",
                        lambda repo, pr, diff, sha="": calls.append((repo, pr, diff)) or {"action": "pass"})

    result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF)

    assert calls == [("pandayv/micro-finance", 1, CODE_DIFF)]
    assert result == {"action": "pass"}


def test_no_escalated_fragments_sends_nothing(monkeypatch):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    clean_diff = '''diff --git a/loans/clean.py b/loans/clean.py
--- a/loans/clean.py
+++ b/loans/clean.py
@@ -1,3 +1,4 @@
+def calculate_payment(principal, rate):
+    return principal * rate
'''
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch_fragments("pandayv/micro-finance", 1, clean_diff)

    mock_client.send_message.assert_not_called()
    assert result["action"] == "pass"


def test_oversized_fragment_is_skipped_not_sent(monkeypatch):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    monkeypatch.setattr(main_module, "SQS_MESSAGE_SIZE_LIMIT", 10)  # force everything over the limit
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF)

    mock_client.send_message.assert_not_called()
    assert result["action"] == "pass"
    assert "too large" in result["reason"]
