"""
Shared Bedrock session + retry configuration, deployed AgentCore copy.

Mirrors src/aws/bedrock_session.py in the main ship-engine repo (see that file's docstring for
the reasoning). Duplicated here, not imported, because this package runs inside AgentCore
Runtime as its own dependency tree with its own import root (`from rag...` only resolves inside
this package), so a shared src/ import is not reachable from here.

This is the more important copy of the two. When the webhook Lambda runs with
SHIP_DETECTOR_MODE=agentcore (the deployed configuration), it makes no direct Bedrock calls: it
only calls invoke_agent_runtime, a control-plane call with a much less restrictive quota than
the model quota. The real Bedrock traffic (Titan embeddings and the model's own completions)
happens inside THIS container, and several container instances run at once because each PR's
flagged fragments are independent jobs. Calls fire immediately and back off on a throttle via
botocore's `adaptive` retry mode (BEDROCK_RETRY_CONFIG below). The SQS event source mapping
caps how many fragments run concurrently (an AWS setting on the fragment-processor Lambda, not
code here).
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
