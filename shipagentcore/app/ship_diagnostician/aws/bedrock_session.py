"""
Shared Bedrock session + retry configuration — deployed AgentCore copy.

Mirrors src/aws/bedrock_session.py in the main ship-engine repo (see that
file's docstring for the full reasoning). Duplicated here, not imported,
for the same reason vector_store.py/chunker.py are duplicated rather than
shared: this package runs inside AgentCore Runtime as its own dependency
tree with its own import root (`from rag...` only resolves inside this
package), so a shared src/ import isn't reachable from here.

This is actually the more important copy of the two: when the SHIP
webhook Lambda runs with SHIP_DIAGNOSTICIAN_MODE=agentcore (the deployed
configuration), the Lambda itself makes no direct Bedrock calls at all —
it only calls invoke_agent_runtime, a control-plane call with a different,
much less restrictive quota than the 10-requests/minute model quota. The
real Bedrock traffic (Titan embeddings + the model's own completions) all
happens inside THIS container.

2026-09-05 — this file used to hand-roll a process-local
SlidingWindowRateLimiter (see git history), flagged in its own docstring
as an open, unsolved problem: it couldn't coordinate across multiple
concurrent AgentCore container instances, so aggregate traffic could
still exceed the real account-wide quota under real concurrent load.
That's now expected and deliberate — SHIP dispatches each PR's flagged
issues as independent, concurrently-processed jobs (see
src/api/main.py's module docstring in the main repo for the full
architecture change), so multiple container instances running at once is
the normal case, not an edge case. Rather than build custom cross-
instance coordination, the fix is AWS's own standard pattern for this
exact scenario: let calls fire immediately, and if AWS responds with a
throttle, back off and retry automatically, via botocore's `adaptive`
retry mode (BEDROCK_RETRY_CONFIG below). Combined with a cap on how many
fragments the SQS-Lambda event source mapping lets run concurrently (an
AWS setting on the fragment-processor Lambda, not code here), this keeps
real throughput near the account's actual limit without a custom
coordinator that — as this file's own prior docstring already
acknowledged — couldn't actually coordinate across instances anyway.
"""

import threading

import boto3
from botocore.config import Config

# 'adaptive' mode observes real throttling responses and paces/backs off
# client-side automatically — the AWS-recommended mode for a fleet of
# clients (here: concurrent AgentCore container instances) sharing one
# account-wide rate limit.
BEDROCK_RETRY_CONFIG = Config(retries={"mode": "adaptive"})

_shared_session: boto3.Session | None = None
_session_lock = threading.Lock()


def bedrock_session() -> boto3.Session:
    """One container-instance-wide boto3 Session, shared by every Bedrock
    caller in this process. Rate-limit handling is per-client, via
    BEDROCK_RETRY_CONFIG — see this module's docstring for why."""
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                _shared_session = boto3.Session()
    return _shared_session
