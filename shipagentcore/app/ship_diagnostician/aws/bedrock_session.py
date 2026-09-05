"""
Shared Bedrock rate-limiting boundary — deployed AgentCore copy.

Mirrors src/aws/bedrock_session.py in the main ship-engine repo (see that
file's docstring for the full reasoning behind finding #45). Duplicated
here, not imported, for the same reason vector_store.py/chunker.py are
duplicated rather than shared: this package runs inside AgentCore Runtime
as its own dependency tree with its own import root (`from rag...` only
resolves inside this package), so a shared src/ import isn't reachable
from here.

This is actually the more important copy of the two: when the SHIP
webhook Lambda runs with SHIP_DIAGNOSTICIAN_MODE=agentcore (the deployed
configuration), the Lambda itself makes no direct Bedrock calls at all —
it only calls invoke_agent_runtime, a control-plane call with a different,
much less restrictive quota than the 10-requests/minute model quota. The
real Bedrock traffic (Titan embeddings + the model's own completions) all
happens inside THIS container, invisible to and unpaced by anything in the
outer Lambda. A rate limiter living only in the Lambda's process (as the
old time.sleep(7) did) never actually paced the thing that matters in
production; this one does, because it sits where the real calls are made.

Scope note or a possible open follow-up, not solved here: this rate
limiter is process-local to one AgentCore container instance. If AgentCore
Runtime scales out to multiple concurrent warm instances under load, each
instance enforces the 10/min quota independently, so aggregate traffic
across instances could still exceed it. True cross-instance coordination
would need a shared external store (DynamoDB/ElastiCache token bucket) —
a bigger architectural change than this finding asked for, and not
something to build unasked; flagged as an open question rather than
built speculatively.
"""

import threading
import time

import boto3

BEDROCK_RUNTIME_RPM = 10  # confirmed real account-wide quota, 2026-09-04


class SlidingWindowRateLimiter:
    """Blocks the calling thread just long enough to keep the last
    `max_calls` calls within `window_seconds` of each other."""

    def __init__(self, max_calls: int, window_seconds: float):
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._call_times: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._call_times = [t for t in self._call_times if t > now - self._window_seconds]
            if len(self._call_times) >= self._max_calls:
                sleep_for = self._call_times[0] + self._window_seconds - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                now = time.monotonic()
                self._call_times = [t for t in self._call_times if t > now - self._window_seconds]
            self._call_times.append(now)


_bedrock_runtime_limiter = SlidingWindowRateLimiter(BEDROCK_RUNTIME_RPM, window_seconds=60.0)
_shared_session: boto3.Session | None = None
_session_lock = threading.Lock()


def _before_send_rate_limit(request=None, **kwargs) -> None:
    _bedrock_runtime_limiter.acquire()
    return None


def bedrock_session() -> boto3.Session:
    """One container-instance-wide boto3 Session, shared by every Bedrock
    caller in this process, with the rate-limit hook registered once."""
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                session = boto3.Session()
                session.events.register("before-send.bedrock-runtime", _before_send_rate_limit)
                _shared_session = session
    return _shared_session
