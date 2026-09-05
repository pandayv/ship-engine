"""
Shared Bedrock session + retry configuration.

Review finding #45 (original fix, 2026-09-04/05): a hand-placed
time.sleep(7) between fragments in main.py's process_pr() loop assumed
exactly one Bedrock call per fragment, which isn't how a tool-using
Diagnostician agent behaves. The first fix moved to a custom, process-
local SlidingWindowRateLimiter shared by every Bedrock caller — real
improvement over a blind sleep, but it never solved (and was always
documented as not solving) coordination ACROSS multiple concurrent
processes/container instances, since each instance's limiter only knows
about its own traffic.

2026-09-05 — replaced with parallel per-fragment dispatch (see
src/api/main.py's module docstring for the full architecture change):
once fragments genuinely run concurrently across multiple Lambda/AgentCore
instances, a pre-emptive local limiter is actively the wrong tool — it
can't see the OTHER instances' traffic, so it can't actually prevent the
account-wide quota (10 requests/minute, confirmed 2026-09-04) from being
exceeded in aggregate, while still adding latency to every call even when
the real limit hasn't been hit.

Real fix: let calls fire immediately, and if AWS responds with a throttle,
back off and retry automatically — AWS's own standard, SDK-native pattern
for exactly this scenario (many concurrent clients sharing one account-
wide limit), via botocore's `adaptive` retry mode. This needs zero custom
coordination code; BEDROCK_RETRY_CONFIG below is applied everywhere a
Bedrock client is built. Combined with a cap on how many fragments the
SQS-Lambda event source mapping lets run concurrently (an AWS setting, not
code — see the fragment-processor Lambda's deployment config), this keeps
real throughput near the account's actual limit without a custom
coordinator that (per the above) couldn't actually coordinate anyway.
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
                # Build into a local var first, same non-atomic-singleton
                # reasoning as finding #10's _get_store() fix: only publish
                # to the module global after setup fully succeeds.
                _shared_session = boto3.Session()
    return _shared_session
