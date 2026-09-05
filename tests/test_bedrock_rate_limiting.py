"""
Tests for finding #45's rate-limiting fix, updated 2026-09-05 for the
parallel per-fragment dispatch redesign (see src/api/main.py's module
docstring for the full architecture change).

The original fix (a hand-rolled, process-local SlidingWindowRateLimiter)
was removed: once fragments genuinely run concurrently across multiple
Lambda/AgentCore instances, a pre-emptive local limiter can't see other
instances' traffic and can't actually prevent the account-wide quota from
being exceeded in aggregate — it was always documented as not solving
that. The replacement is AWS's own standard pattern for many concurrent
clients sharing one rate limit: adaptive retry (botocore's `adaptive`
mode), configured once via BEDROCK_RETRY_CONFIG and applied everywhere a
Bedrock client is built.

Includes structural checks (reading source as text, same technique as
tests/test_taxonomy_consistency.py) that both the in-process copy AND the
deployed AgentCore copy actually apply BEDROCK_RETRY_CONFIG everywhere
they build a Bedrock client — catching a future edit that quietly reverts
to an unconfigured boto3.client("bedrock-runtime") in either copy, which
would silently reintroduce unhandled-throttle failures under concurrent
load with no error anywhere obvious.
"""

from pathlib import Path

from src.aws.bedrock_session import BEDROCK_RETRY_CONFIG, bedrock_session

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_DIAGNOSTICIAN_SOURCE = (REPO_ROOT / "src" / "agents" / "diagnostician.py").read_text()
MAIN_VECTOR_STORE_SOURCE = (REPO_ROOT / "src" / "rag" / "vector_store.py").read_text()
DEPLOYED_DIAGNOSTICIAN_SOURCE = (
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "main.py"
).read_text()
DEPLOYED_VECTOR_STORE_SOURCE = (
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag" / "vector_store.py"
).read_text()
DEPLOYED_BEDROCK_SESSION_SOURCE = (
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "aws" / "bedrock_session.py"
).read_text()


def test_bedrock_retry_config_uses_adaptive_mode():
    # 'adaptive' is the specific mode that observes real throttling
    # responses and paces/backs off client-side automatically - the
    # AWS-recommended pattern for many concurrent clients sharing one
    # account-wide rate limit. Read directly off the real Config object,
    # not assumed from how it was constructed.
    assert BEDROCK_RETRY_CONFIG.retries["mode"] == "adaptive"


def test_bedrock_session_is_a_process_wide_singleton():
    # Same session object every call - avoids repeating session/credential
    # resolution per caller, even though rate-limit handling itself is now
    # per-client (BEDROCK_RETRY_CONFIG), not on the session.
    assert bedrock_session() is bedrock_session()


def test_deployed_bedrock_session_also_exports_adaptive_retry_config():
    # The deployed copy is a separate module (own dependency tree, can't
    # import the main repo's copy) - read as text since it can't be
    # imported into this venv (bedrock_agentcore isn't installed here,
    # same reasoning as test_taxonomy_consistency.py).
    assert 'Config(retries={"mode": "adaptive"})' in DEPLOYED_BEDROCK_SESSION_SOURCE


def test_main_bedrock_model_uses_adaptive_retry_config():
    assert "BEDROCK_RETRY_CONFIG" in MAIN_DIAGNOSTICIAN_SOURCE
    assert "boto_client_config=BEDROCK_RETRY_CONFIG" in MAIN_DIAGNOSTICIAN_SOURCE


def test_deployed_bedrock_model_uses_adaptive_retry_config():
    assert "BEDROCK_RETRY_CONFIG" in DEPLOYED_DIAGNOSTICIAN_SOURCE
    assert "boto_client_config=BEDROCK_RETRY_CONFIG" in DEPLOYED_DIAGNOSTICIAN_SOURCE


def test_main_vector_store_embeddings_use_adaptive_retry_config():
    assert "config=BEDROCK_RETRY_CONFIG" in MAIN_VECTOR_STORE_SOURCE
    assert 'client("bedrock-runtime")' not in MAIN_VECTOR_STORE_SOURCE  # must always pass a config


def test_deployed_vector_store_embeddings_use_adaptive_retry_config():
    assert "config=BEDROCK_RETRY_CONFIG" in DEPLOYED_VECTOR_STORE_SOURCE
    assert 'client("bedrock-runtime")' not in DEPLOYED_VECTOR_STORE_SOURCE


def test_main_py_dispatches_fragments_instead_of_self_invoking_lambda():
    # The old design (self-invoke the same Lambda with the whole diff via
    # InvocationType="Event") is fully replaced by per-fragment SQS
    # dispatch - assert the old marker/self-invoke machinery is gone and
    # the new dispatch function is what the webhook route actually calls.
    main_source = (REPO_ROOT / "src" / "api" / "main.py").read_text()
    assert "ASYNC_MARKER" not in main_source
    assert "_dispatch_fragments(repo_full_name, pr_number, code_diff)" in main_source
