"""
Tests for parallel per-fragment dispatch (see src/api/main.py's module docstring).

_dispatch_fragments() splits the diff, scans each fragment, and sends one SQS message per
escalated fragment, so each fragment becomes its own independent, concurrently-processable job
instead of every fragment being processed sequentially inside one Lambda invocation bounded by
Lambda's hard 900-second timeout.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

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


class Lifecycle:
    """Everything the webhook leg does besides queueing: the statuses it posts, the review it
    records, the blocked findings it releases."""

    def __init__(self):
        self.statuses, self.syncs, self.ledgers, self.released = [], [], [], []


@pytest.fixture(autouse=True)
def lifecycle(monkeypatch):
    seen = Lifecycle()
    monkeypatch.setattr(main_module, "post_commit_status", lambda repo, pr, state, desc, sha="", trust_head=False:
                        seen.statuses.append((state, desc)) or True)
    monkeypatch.setattr(main_module, "sync_pr_check", lambda *a, **k: seen.syncs.append((a, k)) or True)
    monkeypatch.setattr(main_module, "begin_review", lambda repo, pr, sha, keys: seen.ledgers.append((sha, list(keys))) or True)
    monkeypatch.setattr(main_module, "release_fixed_blocks",
                        lambda repo, pr, keys: seen.released.append(set(keys)) or 0)
    monkeypatch.setattr(main_module, "refresh_summary", lambda *a, **k: None)
    return seen


def test_sends_one_sqs_message_per_escalated_fragment_only(monkeypatch, lifecycle):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client) as mock_boto_client:
        result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF, "sha1")

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
    assert lifecycle.statuses == []                 # still reviewing: the fragment processors report
    assert len(lifecycle.ledgers) == 1 and lifecycle.ledgers[0][0] == "sha1" and len(lifecycle.ledgers[0][1]) == 1


def test_falls_back_to_process_pr_when_no_queue_configured(monkeypatch):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "")
    calls = []
    monkeypatch.setattr(main_module, "process_pr",
                        lambda repo, pr, diff, sha="": calls.append((repo, pr, diff)) or {"action": "pass"})

    result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF)

    assert calls == [("pandayv/micro-finance", 1, CODE_DIFF)]
    assert result == {"action": "pass"}


CLEAN_DIFF = '''diff --git a/loans/clean.py b/loans/clean.py
--- a/loans/clean.py
+++ b/loans/clean.py
@@ -1,3 +1,4 @@
+def calculate_payment(principal, rate):
+    return principal * rate
'''


def test_no_escalated_fragments_sends_nothing_and_reports_the_pass(monkeypatch, lifecycle):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch_fragments("pandayv/micro-finance", 1, CLEAN_DIFF, "sha1")

    mock_client.send_message.assert_not_called()
    assert result["action"] == "pass"
    assert len(lifecycle.syncs) == 1 and lifecycle.ledgers == []      # the pending status is replaced
    assert lifecycle.released == [set()]                                 # nothing flagged: blocked findings may be released


def test_a_blocked_finding_is_released_only_when_its_code_is_gone(monkeypatch, lifecycle):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    with patch("boto3.client", return_value=MagicMock()):
        _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF, "sha1")
    from src.storage.alert_store import fragment_key_for
    keys = lifecycle.released[0]
    assert len(keys) == 1 and next(iter(keys)) == lifecycle.ledgers[0][1][0]
    assert next(iter(keys)) == fragment_key_for("loans/apply.py", main_module._escalate(CODE_DIFF)[1][0]["isolated_fragment"])


def test_oversized_fragment_is_never_passed_it_is_reported_as_an_error(monkeypatch, lifecycle, caplog):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    monkeypatch.setattr(main_module, "SQS_MESSAGE_SIZE_LIMIT", 10)  # force everything over the limit
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client), caplog.at_level("ERROR"):
        result = _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF, "sha1")

    mock_client.send_message.assert_not_called()
    assert result["action"] == "error" and "too large" in result["reason"]
    assert lifecycle.statuses == [("error", "SHIP could not review 1 fragment: too large to queue")]
    assert "NOT reviewed" in caplog.text
    assert len(lifecycle.ledgers[0][1]) == 1          # it stays expected, so the check cannot turn green on it


def test_some_fragments_too_large_still_queues_the_rest_and_flags_the_error(monkeypatch, lifecycle):
    two = CODE_DIFF + CODE_DIFF.replace("loans/apply.py", "loans/other.py").replace("applicant", "customer")
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    sizes = iter([100, 10_000_000])
    real = json.dumps
    monkeypatch.setattr(main_module.json, "dumps", lambda payload: real(payload) + ("x" * next(sizes)))
    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        result = _dispatch_fragments("pandayv/micro-finance", 1, two, "sha1")
    assert mock_client.send_message.call_count == 1 and lifecycle.statuses[0][0] == "error"
    assert result["action"] == "accepted" and "too large" in result["reason"]


def test_a_queueing_failure_is_reported_on_the_pull_request_and_surfaces_as_an_error(monkeypatch, lifecycle):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")
    mock_client = MagicMock()
    mock_client.send_message.side_effect = RuntimeError("sqs down")
    with patch("boto3.client", return_value=mock_client), pytest.raises(HTTPException) as caught:
        _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF, "sha1")
    assert caught.value.status_code == 502 and lifecycle.statuses[0][0] == "error"


def test_a_failure_to_release_blocked_findings_never_fails_the_review(monkeypatch, lifecycle):
    monkeypatch.setattr(main_module, "FRAGMENT_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123/fragment-queue")

    def broken(repo, pr, keys):
        raise RuntimeError("table down")

    monkeypatch.setattr(main_module, "release_fixed_blocks", broken)
    with patch("boto3.client", return_value=MagicMock()):
        assert _dispatch_fragments("pandayv/micro-finance", 1, CODE_DIFF, "sha1")["action"] == "accepted"
