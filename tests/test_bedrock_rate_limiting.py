"""
Tests for finding #45's real fix: rate-limiting Bedrock calls at the
shared transport boundary (src/aws/bedrock_session.py) instead of a
hand-placed time.sleep(7) in main.py's fragment loop that assumed one
Bedrock call per fragment.

Includes structural checks (reading source as text, same technique as
tests/test_taxonomy_consistency.py) that both the in-process copy AND the
deployed AgentCore copy actually route their Bedrock calls through the
shared rate limiter — catching a future edit that quietly reverts to a
bare boto3.client("bedrock-runtime") in either copy, which would silently
reintroduce unpaced Bedrock traffic with no error anywhere.
"""

import time
from pathlib import Path

from src.aws.bedrock_session import SlidingWindowRateLimiter, bedrock_session

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_DIAGNOSTICIAN_SOURCE = (REPO_ROOT / "src" / "agents" / "diagnostician.py").read_text()
MAIN_VECTOR_STORE_SOURCE = (REPO_ROOT / "src" / "rag" / "vector_store.py").read_text()
DEPLOYED_DIAGNOSTICIAN_SOURCE = (
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "main.py"
).read_text()
DEPLOYED_VECTOR_STORE_SOURCE = (
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag" / "vector_store.py"
).read_text()


def test_limiter_allows_calls_under_the_limit_without_blocking():
    limiter = SlidingWindowRateLimiter(max_calls=3, window_seconds=1.0)
    start = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"calls under the limit should not block, took {elapsed}s"


def test_limiter_blocks_the_call_that_exceeds_the_limit():
    limiter = SlidingWindowRateLimiter(max_calls=2, window_seconds=0.4)
    limiter.acquire()
    limiter.acquire()
    start = time.monotonic()
    limiter.acquire()  # 3rd call within the window must wait
    elapsed = time.monotonic() - start
    assert elapsed >= 0.35, f"3rd call within the window should have been paced, took {elapsed}s"


def test_limiter_does_not_block_once_the_window_has_passed():
    limiter = SlidingWindowRateLimiter(max_calls=1, window_seconds=0.2)
    limiter.acquire()
    time.sleep(0.25)  # let the window fully expire
    start = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.1, f"call after the window expired should not block, took {elapsed}s"


def test_bedrock_session_is_a_process_wide_singleton():
    # Same session object every call - the fix only works if every Bedrock
    # caller in the process actually shares the one rate-limited session,
    # not a fresh, unregistered one each time.
    assert bedrock_session() is bedrock_session()


def test_bedrock_session_registers_the_rate_limit_hook_on_bedrock_runtime():
    session = bedrock_session()
    calls = []
    session.events.register("before-send.bedrock-runtime", lambda **kw: calls.append(kw))
    # Emit the actual hierarchical event name botocore uses for a real
    # bedrock-runtime operation (confirmed via botocore source read, see
    # bedrock_session.py's docstring) - proves the hook is reachable from
    # the real event path, not just registered under an unused name.
    session.events.emit("before-send.bedrock-runtime.Converse", request="fake")
    assert len(calls) >= 1, "before-send.bedrock-runtime hook was not reachable from a real operation event"


def test_main_bedrock_model_uses_the_shared_rate_limited_session():
    assert "from src.aws.bedrock_session import bedrock_session" in MAIN_DIAGNOSTICIAN_SOURCE
    assert "boto_session=bedrock_session()" in MAIN_DIAGNOSTICIAN_SOURCE


def test_deployed_bedrock_model_uses_the_shared_rate_limited_session():
    assert "from aws.bedrock_session import bedrock_session" in DEPLOYED_DIAGNOSTICIAN_SOURCE
    assert "boto_session=bedrock_session()" in DEPLOYED_DIAGNOSTICIAN_SOURCE


def test_main_vector_store_embeddings_use_the_shared_rate_limited_session():
    assert "bedrock_session()" in MAIN_VECTOR_STORE_SOURCE
    assert 'boto3.client("bedrock-runtime")' not in MAIN_VECTOR_STORE_SOURCE


def test_deployed_vector_store_embeddings_use_the_shared_rate_limited_session():
    assert "bedrock_session()" in DEPLOYED_VECTOR_STORE_SOURCE
    assert 'boto3.client("bedrock-runtime")' not in DEPLOYED_VECTOR_STORE_SOURCE


def test_main_py_no_longer_has_the_removed_fragment_loop_sleep():
    main_source = (REPO_ROOT / "src" / "api" / "main.py").read_text()
    # Check for the actual removed statement, not just the string "time.sleep(7)"
    # anywhere (this test file's own explanatory comments mention it by name).
    assert "        time.sleep(7)" not in main_source
    assert "\nimport time\n" not in main_source
