"""
Shared Bedrock rate-limiting boundary.

Review finding #45: a hand-placed time.sleep(7) between fragments in
main.py's process_pr() loop assumed exactly one Bedrock call per fragment.
That's not how a tool-using Diagnostician agent behaves: a single
diagnose_in_process() call can issue several independent Bedrock calls the
outer sleep never sees (the RAG tool's embedding lookup, callable more than
once per agent turn, plus the model's own completion call(s) across
multi-turn tool use), and VectorStore.build()'s cold-start embedding burst
(one sequential invoke_model call per corpus chunk) had zero pacing at all.
The "stay under 10/min" guarantee only held if each fragment triggered
exactly one Bedrock call - not a safe assumption once retries or multiple
tool calls are in play.

Real fix: rate-limit at the shared transport boundary instead of guessing a
per-fragment delay. Every Bedrock call this process makes - Titan
embeddings via _embed_bedrock() and LLM completions via Strands'
BedrockModel - goes through a boto3 client built from the ONE shared
Session returned by bedrock_session(). That Session has a `before-send`
hook registered for the bedrock-runtime service (confirmed via botocore
source read, not guessed: botocore.endpoint.Endpoint._do_get_response()
emits `before-send.{service_id}.{operation_name}` immediately before every
real HTTP send attempt, INCLUDING retries, since the retry loop re-enters
this same method on each attempt - so this hook paces retries too, which
the old fragment-loop sleep never could). The hook enforces the real,
confirmed account-wide quota (10 requests/minute, cross-region, confirmed
2026-09-04 - see ship_roadmap.md) as a sliding window over actual request
timestamps - correct regardless of how many calls a single fragment or a
cold-start corpus build ends up issuing.
"""

import threading
import time

import boto3

BEDROCK_RUNTIME_RPM = 10  # confirmed real account-wide quota, 2026-09-04


class SlidingWindowRateLimiter:
    """Blocks the calling thread just long enough to keep the last
    `max_calls` calls within `window_seconds` of each other. Not a fixed
    delay: a burst of calls after a quiet period proceeds immediately, and
    only pacing over the actual limit incurs a wait."""

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
    return None  # None lets botocore actually send the request as normal


def bedrock_session() -> boto3.Session:
    """
    One process-wide boto3 Session, shared by every Bedrock caller
    (embeddings and LLM completions alike), with the rate-limit hook
    registered exactly once. A fresh boto3.Session() per caller would each
    carry their own unregistered hook - the whole point of this finding's
    fix is one shared limiter that every caller actually goes through, so
    the account-wide quota is enforced against real combined traffic, not
    per-caller guesses.
    """
    global _shared_session
    if _shared_session is None:
        with _session_lock:
            if _shared_session is None:
                # Build into a local var first, same non-atomic-singleton
                # reasoning as finding #10's _get_store() fix: only publish
                # to the module global after setup fully succeeds.
                session = boto3.Session()
                session.events.register("before-send.bedrock-runtime", _before_send_rate_limit)
                _shared_session = session
    return _shared_session
