"""
Shared Bedrock session + retry configuration.

Fragments of a PR run concurrently across Lambda and AgentCore instances, and all of them draw
on one account-wide Bedrock request quota. A local, pre-emptive rate limiter cannot see the
other instances' traffic, so it cannot keep the aggregate under the quota, and it adds latency
to every call even when the limit has not been hit.

So calls fire immediately, and if AWS responds with a throttle the client backs off and
retries automatically. That is AWS's SDK-native pattern for many concurrent clients sharing
one limit: botocore's `adaptive` retry mode. It needs no custom coordination code.
BEDROCK_RETRY_CONFIG below is applied everywhere a Bedrock client is built. Combined with a cap
on how many fragments the SQS event source mapping lets run concurrently (an AWS setting on
the fragment-processor Lambda, not code), this keeps throughput near the account's limit.
"""

import threading

import boto3
from botocore.config import Config

# 'adaptive' mode observes real throttling responses and paces/backs off
# client-side automatically — the AWS-recommended mode for a fleet of
# clients sharing one account-wide rate limit. Passed to every Bedrock
# client this codebase builds (both the RAG embedding client and the
# Strands BedrockModel's own client).
BEDROCK_RETRY_CONFIG = Config(retries={"mode": "adaptive"})

_shared_session: boto3.Session | None = None
_session_lock = threading.Lock()


def bedrock_session() -> boto3.Session:
    """
    One process-wide boto3 Session, shared by every Bedrock caller.
    Session construction itself is cheap-but-not-free (env/credential
    resolution); sharing one instance avoids repeating that per caller.
    Actual rate-limit handling is per-client, via BEDROCK_RETRY_CONFIG —
    see this module's docstring for why that's the right layer now.
    """
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                # Build into a local var first and publish to the module global only after setup
                # fully succeeds.
                _shared_session = boto3.Session()
    return _shared_session
