"""
Regression test for a real bug caught live during finding #45's deployment
verification (2026-09-05): diagnose_via_agentcore()'s boto3 client used
botocore's default 60s read timeout, but a real invoke_agent_runtime call
can legitimately take several minutes once the deployed container's own
Bedrock rate limiter is pacing a cold-start embedding burst plus the
model's completion calls (directly observed: 190s-308s in real runs).
The timeout fired mid-call, was silently swallowed by process_pr()'s
per-fragment error handler, and the async Lambda invocation "succeeded"
in CloudWatch while producing zero alerts - no error visible anywhere
until logging was added to that handler.

This test would have caught the bug on the first `pytest` run, without
needing a live multi-minute reproduction to notice a 60s timeout is too
short for a call that predictably takes longer than that.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.diagnostician import diagnose_via_agentcore


def test_agentcore_client_uses_a_read_timeout_with_real_margin_under_the_lambda_timeout(monkeypatch):
    monkeypatch.setenv(
        "SHIP_AGENTCORE_RUNTIME_ARN",
        "arn:aws:bedrock-agentcore:us-west-2:680160265218:runtime/fake-runtime",
    )

    fake_response = {"response": MagicMock(read=lambda: b'{"matched": false}')}
    with patch("boto3.client") as mock_boto_client:
        mock_client = MagicMock()
        mock_client.invoke_agent_runtime.return_value = fake_response
        mock_boto_client.return_value = mock_client

        diagnose_via_agentcore("isolated fragment text")

        assert mock_boto_client.call_count == 1
        _, kwargs = mock_boto_client.call_args
        config = kwargs["config"]
        # Real margin under this Lambda's 600s function timeout - not the
        # botocore default of 60s, which is shorter than real observed
        # call durations (190s-308s) once rate limiting is pacing calls.
        assert config.read_timeout >= 300, "read_timeout must have real margin over observed 190s-308s call durations"
        assert config.read_timeout < 600, "read_timeout must stay under the Lambda's own 600s function timeout"
        # A retry on timeout would silently double the wait instead of
        # failing fast for process_pr()'s per-fragment handler to catch.
        assert config.retries["max_attempts"] == 1


def test_agentcore_client_raises_clearly_when_arn_is_unset(monkeypatch):
    monkeypatch.delenv("SHIP_AGENTCORE_RUNTIME_ARN", raising=False)
    with pytest.raises(RuntimeError, match="SHIP_AGENTCORE_RUNTIME_ARN"):
        diagnose_via_agentcore("isolated fragment text")
