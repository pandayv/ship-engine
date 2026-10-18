"""
Tests for process_fragment() (src/api/main.py) and the SQS-triggered
fragment_lambda_handler.py entrypoint, part of the 2026-09-05 parallel
per-fragment dispatch redesign.

process_fragment() deliberately does NOT catch its own exceptions - the
two callers need different policies. process_pr()'s sequential loop
catches per-fragment (finding #8: one failure shouldn't abort the rest of
the same run); fragment_lambda_handler.py wants the exception to
propagate so SQS's own retry/DLQ mechanism handles it instead.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import fragment_lambda_handler
from src.agents.detector import DetectorOutput
from src.api.main import process_fragment


def _matched_output(taxonomy_id="PIIE-001", risk_score=9.0):
    return DetectorOutput(
        matched=True, taxonomy_id=taxonomy_id, risk_score=risk_score,
        plain_english_summary="summary", citation="citation", remediation_patch="patch",
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """process_fragment() writes findings back to the pull request. Stub
    both calls for every test in this module so none of them can reach
    GitHub — an unstubbed one would silently depend on GITHUB_TOKEN being
    absent from the environment to stay offline."""
    monkeypatch.setattr("src.api.main.post_pr_comment", lambda *a, **k: True)
    monkeypatch.setattr("src.api.main.sync_pr_check", lambda *a, **k: True)
    monkeypatch.setattr("src.api.main.fragment_reported", lambda *a: True)
    monkeypatch.setattr("src.api.main.claim_step", lambda *a: False)      # delivery steps are tested in test_alert_comment_once.py
    # Also the comment builder: it reads real Alert fields, and it is
    # evaluated as an argument to post_pr_comment before the stub above
    # ever sees it. Its own output is covered in test_github_writeback.py.
    monkeypatch.setattr("src.api.main.comment_for_finding", lambda alert: "stub comment")


def test_process_fragment_stores_an_alert_when_frozen(monkeypatch):
    monkeypatch.setattr("src.api.main.detect", lambda text: _matched_output())
    calls = []

    class _FakeAlert:
        alert_id = "abc123"

    monkeypatch.setattr("src.api.main.put_alert", lambda **kwargs: calls.append(kwargs) or _FakeAlert())

    result = process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "logger.info(applicant.ssn)")

    assert result["action"] == "freeze"
    assert result["alert_id"] == "abc123"
    assert len(calls) == 1
    assert calls[0]["file"] == "loans/apply.py"
    assert calls[0]["fragment_text"] == "logger.info(applicant.ssn)"


def test_process_fragment_stores_a_non_blocking_alert_for_a_middle_band_finding(monkeypatch):
    # The three-band model's real integration point: a confirmed finding
    # scored between PIIE-001's review floor (5.0) and its blocking bar
    # (7.0) must still be PERSISTED for a human, but recorded as
    # non-blocking. Under the old two-band model this score produced no
    # alert row at all.
    monkeypatch.setattr("src.api.main.detect", lambda text: _matched_output(risk_score=6.0))
    calls = []

    class _FakeAlert:
        alert_id = "def456"

    monkeypatch.setattr("src.api.main.put_alert", lambda **kwargs: calls.append(kwargs) or _FakeAlert())

    result = process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "logger.info(applicant.ssn)")

    assert result["action"] == "review"
    assert result["blocks_build"] is False
    assert result["alert_id"] == "def456"
    assert len(calls) == 1
    assert calls[0]["severity"] == "review"


def test_process_fragment_records_a_blocking_finding_as_blocking(monkeypatch):
    monkeypatch.setattr("src.api.main.detect", lambda text: _matched_output(risk_score=9.0))
    calls = []

    class _FakeAlert:
        alert_id = "abc123"

    monkeypatch.setattr("src.api.main.put_alert", lambda **kwargs: calls.append(kwargs) or _FakeAlert())

    result = process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "logger.info(applicant.ssn)")

    assert result["blocks_build"] is True
    assert calls[0]["severity"] == "blocking"


def test_process_fragment_does_not_store_an_alert_below_the_review_floor(monkeypatch):
    # A confirmed but trivial finding shouldn't clutter the human's queue.
    monkeypatch.setattr("src.api.main.detect", lambda text: _matched_output(risk_score=2.0))
    calls = []
    monkeypatch.setattr("src.api.main.put_alert", lambda **kwargs: calls.append(kwargs))

    result = process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "logger.info(applicant.ssn)")

    assert result["action"] == "log_warning"
    assert result["alert_id"] is None
    assert calls == []


def test_process_fragment_raises_instead_of_swallowing_errors(monkeypatch):
    def _raise(text):
        raise RuntimeError("simulated transient Bedrock failure")

    monkeypatch.setattr("src.api.main.detect", _raise)

    with pytest.raises(RuntimeError, match="simulated transient Bedrock failure"):
        process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "logger.info(applicant.ssn)")


def test_fragment_lambda_handler_calls_process_fragment_with_message_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(fragment_lambda_handler, "process_fragment", lambda **kwargs: calls.append(kwargs))

    event = {
        "Records": [
            {
                "body": json.dumps({
                    "repo_full_name": "pandayv/micro-finance",
                    "pr_number": 42,
                    "file": "loans/apply.py",
                    "isolated_fragment": "logger.info(applicant.ssn)",
                    "head_sha": "deadbeef",
                })
            }
        ]
    }
    fragment_lambda_handler.handler(event, None)

    assert calls == [{
        "repo_full_name": "pandayv/micro-finance",
        "pr_number": 42,
        "file": "loans/apply.py",
        "isolated_fragment": "logger.info(applicant.ssn)",
        "head_sha": "deadbeef",
    }]


def test_fragment_lambda_handler_tolerates_a_message_without_head_sha(monkeypatch):
    # head_sha was added to the message shape after the queue was already
    # live. A message enqueued by the previous version must still process
    # rather than KeyError into the dead-letter queue.
    calls = []
    monkeypatch.setattr(fragment_lambda_handler, "process_fragment", lambda **kwargs: calls.append(kwargs))

    fragment_lambda_handler.handler({"Records": [{"body": json.dumps({
        "repo_full_name": "pandayv/micro-finance", "pr_number": 42,
        "file": "loans/apply.py", "isolated_fragment": "logger.info(applicant.ssn)",
    })}]}, None)

    assert calls[0]["head_sha"] == ""


def test_fragment_lambda_handler_propagates_process_fragment_failures(monkeypatch):
    # Deliberate: a genuine failure must propagate out of the handler so
    # Lambda reports the invocation as failed and SQS's own retry/DLQ
    # mechanism (maxReceiveCount, then the dead-letter queue) takes over -
    # not swallowed here, which would silently drop a failed fragment with
    # no retry and no record it ever failed.
    def _raise(**kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(fragment_lambda_handler, "process_fragment", _raise)

    event = {"Records": [{"body": json.dumps({
        "repo_full_name": "pandayv/micro-finance", "pr_number": 1,
        "file": "x.py", "isolated_fragment": "y",
    })}]}

    with pytest.raises(RuntimeError, match="simulated failure"):
        fragment_lambda_handler.handler(event, None)


# --- every outcome reports to the pull request ---


@pytest.fixture
def checks(monkeypatch):
    seen = []
    monkeypatch.setattr("src.api.main.sync_pr_check", lambda *a, **k: seen.append((a, k)) or True)
    return seen


def _no_alert(monkeypatch):
    monkeypatch.setattr("src.api.main.put_alert", lambda **kwargs: pytest.fail("no finding should be stored"))


def test_a_fragment_the_detector_clears_still_reports_to_the_pull_request(monkeypatch, checks):
    # The webhook posts "pending" on accepting a commit; a clean fragment must still contribute,
    # or a clean commit would stay pending forever.
    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(matched=False))
    _no_alert(monkeypatch)
    result = process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "frag", "sha1")
    assert result["action"] == "pass" and len(checks) == 1
    assert checks[0][0] == ("pandayv/micro-finance", 1, "sha1") and checks[0][1] == {"final": True}


def test_a_fragment_below_the_review_floor_reports_too(monkeypatch, checks):
    monkeypatch.setattr("src.api.main.detect", lambda text: _matched_output(risk_score=2.0))
    _no_alert(monkeypatch)
    assert process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "frag", "sha1")["action"] == "log_warning"
    assert len(checks) == 1


def test_the_check_is_not_final_while_other_fragments_of_the_review_are_outstanding(monkeypatch, checks):
    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(matched=False))
    monkeypatch.setattr("src.api.main.fragment_reported", lambda *a: False)
    process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "frag", "sha1")
    assert checks[0][1] == {"final": False}


def test_an_unreadable_ledger_leaves_the_check_pending_instead_of_guessing(monkeypatch, checks, caplog):
    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(matched=False))

    def broken(*a):
        raise RuntimeError("throttled")

    monkeypatch.setattr("src.api.main.fragment_reported", broken)
    with caplog.at_level("ERROR"):
        process_fragment("pandayv/micro-finance", 1, "loans/apply.py", "frag", "sha1")
    assert checks[0][1] == {"final": False} and "review ledger" in caplog.text


def test_the_local_test_repo_never_consults_the_ledger(monkeypatch, checks):
    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(matched=False))
    monkeypatch.setattr("src.api.main.fragment_reported", lambda *a: pytest.fail("no ledger for local runs"))
    process_fragment("local-test", 0, "loans/apply.py", "frag", "")
    assert checks[0][1] == {"final": True}
