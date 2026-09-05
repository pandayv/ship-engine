"""
FastAPI webhook ingestion — wires Screener -> Diagnostician -> Triage -> alert_store.

Deliberately kept as a plain FastAPI service rather than API Gateway (see
ship_roadmap.md's AWS-native stack section for why FastAPI over Lambda's
usual API Gateway pairing) — this same app is wrapped for Lambda via
Mangum (see lambda_handler.py) rather than needing a separate deployment.

IMPORTANT architecture note (found + fixed 2026-09-04, REPLACED 2026-09-05
— see next section): GitHub's webhook delivery does NOT wait several
minutes for a response, so /api/v1/webhook does the CHEAP work only
(signature check, parse payload, a whole-diff Screener precheck) and
returns immediately.

2026-09-05 — parallel per-fragment dispatch (replaces the original single-
Lambda async design): a live end-to-end test proved the original design —
one Lambda invocation sequentially diagnosing every escalated fragment in
a PR — could be silently killed mid-review by AWS Lambda's hard, non-
negotiable 900-second function timeout the moment a PR had 3+ escalated
fragments across categories (2 fragments alone consumed the full budget
in the observed failure; see SHIP_REVIEW_DISPOSITIONS.md for the full
writeup). Nothing crashed or errored visibly — the review just silently
stopped short. Since a judge or a real dense PR could hit this at any
time, not just during a demo, this was not acceptable to leave as-is.

Fix: each escalated fragment is now its own independent, retryable unit
of work, dispatched via SQS (_dispatch_fragments()) to a separate
fragment-processing Lambda (see fragment_lambda_handler.py at the repo
root) instead of being processed sequentially inside one Lambda
invocation. Fragments for the same PR can run concurrently, so a multi-
issue PR takes roughly as long as its slowest single fragment, not the
sum of all of them — and no single fragment's slow diagnosis risks
starving the ones after it of remaining time budget. process_pr() is kept
as the synchronous, sequential fallback used when no queue is configured
(SHIP_FRAGMENT_QUEUE_URL unset) — local dev and the test suite keep
working with zero AWS dependency, same convenience the old
LAMBDA_FUNCTION_NAME-unset fallback provided.

Rate-limit handling: multiple fragments running concurrently means
multiple Bedrock calls can happen at once, which could collectively
exceed the account-wide Bedrock quota (10 requests/minute) even though no
single fragment is doing anything wrong. Rather than build custom cross-
instance coordination (the old SlidingWindowRateLimiter in
src/aws/bedrock_session.py never actually solved this — it was always
documented as process-local only), the fix is AWS's own standard pattern
for this exact scenario: let calls fire immediately, and if AWS responds
with a throttle, back off and retry automatically (botocore's `adaptive`
retry mode, configured once on the shared Bedrock client). Combined with
a cap on how many fragments the SQS-Lambda event source mapping allows to
run concurrently (a single AWS setting, not custom code), this keeps real
throughput near the account's actual limit without a fragile custom
coordinator.

2026-09-05 architecture-review fix pass (see SHIP_REVIEW_DISPOSITIONS.md
for the full plain-English writeup of every finding):
- #1: signature verification now fails CLOSED by default (was fail-open
  when GITHUB_WEBHOOK_SECRET was unset) — set SHIP_ALLOW_UNSIGNED=true to
  explicitly opt into the old dev-convenience behavior.
- #44: the local-test `{"code_diff": ...}` payload path no longer gets
  special-cased synchronous processing inline in the request — it goes
  through the same dispatch as a real PR event, closing the same
  GitHub-timeout-shaped failure mode for any caller producing that shape.
- #7: added a minimal repo allowlist (SHIP_ALLOWED_REPOS) — a payload
  can't make this service spend its GitHub token / Bedrock quota against
  an arbitrary repo of the caller's choosing.
- #8: a single fragment's exception no longer aborts every fragment after
  it in the same PR — caught and recorded per-fragment instead (in the
  process_pr() fallback path; in the SQS path, each fragment is already
  an independent job by construction, so this concern doesn't apply the
  same way — see fragment_lambda_handler.py).
- #19: the whole-diff precheck and the per-fragment Screener scan used to
  happen twice (once in the fast leg, once again inside process_pr()) —
  now the per-fragment scan happens exactly once, in the fast leg, as a
  natural consequence of needing to know what to enqueue.
- #60: file path now threaded through into put_alert() so a multi-file
  PR's alerts are traceable back to which file triggered each one.
- #61: message/payload size is checked against the relevant AWS limit
  before attempting the call, instead of letting an oversized fragment
  raise an uncaught ClientError inside the "fast" synchronous leg.
"""

import hashlib
import hmac
import json
import logging
import os

import requests
from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.diagnostician import diagnose
from src.agents.screener import isolate_fragment, scan, split_diff_into_fragments
from src.agents.triage import BuildAction, route
from src.api.dashboard import router as dashboard_router
from src.api.github_client import extract_pr_ref, fetch_pr_diff, is_pr_event
from src.storage.alert_store import put_alert

app = FastAPI(title="SHIP")
app.include_router(dashboard_router)

log = logging.getLogger(__name__)
# Real bug caught during end-to-end verification (2026-09-05): AWS Lambda's
# Python runtime pre-attaches its own handler to the root logger before
# user code runs. logging.basicConfig() is documented to be a no-op if the
# root logger already has handlers (unless force=True) - so this silently
# did nothing in the deployed Lambda, even though it worked fine locally
# (no pre-existing handler there), which is exactly why this went
# unnoticed: local testing gave false confidence. Only ERROR-level
# log.exception() calls were ever actually visible in CloudWatch; the
# INFO-level "process_pr start/done" and "fragment result" lines this
# session's earlier #45 follow-up fix added for debuggability were never
# reaching CloudWatch at all. Setting the level directly on both this
# logger and the root logger works regardless of pre-existing handlers -
# confirmed by a real Lambda invocation after this fix, not assumed.
log.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOW_UNSIGNED = os.environ.get("SHIP_ALLOW_UNSIGNED", "").lower() == "true"
FRAGMENT_QUEUE_URL = os.environ.get("SHIP_FRAGMENT_QUEUE_URL", "")
SQS_MESSAGE_SIZE_LIMIT = 250_000  # bytes; AWS's real cap is 256KB, small margin for JSON overhead
ALLOWED_REPOS = {r.strip() for r in os.environ.get("SHIP_ALLOWED_REPOS", "pandayv/micro-finance").split(",") if r.strip()}


def _verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    if not GITHUB_WEBHOOK_SECRET:
        if ALLOW_UNSIGNED:
            return  # explicit local-dev opt-out, not the default
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured on the server")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
    expected = "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def process_fragment(repo_full_name: str, pr_number: int, file: str, isolated_fragment: str) -> dict:
    """
    Diagnose, route, and (if frozen) store the verdict for ONE already-
    escalated, already-isolated code fragment. This is the actual unit of
    work — extracted 2026-09-05 so there's exactly one implementation of
    "what happens to one fragment" shared by process_pr()'s sequential
    local-fallback loop below and the SQS-triggered fragment processor
    (fragment_lambda_handler.py at the repo root), instead of two copies
    that could drift apart.

    Deliberately does NOT catch its own exceptions — raises on failure.
    The two callers need different policies for that: process_pr()'s loop
    catches per-fragment so one failure doesn't abort the rest of the same
    sequential run (finding #8); the SQS handler wants the exception to
    propagate so SQS's own retry/DLQ mechanism handles it instead.
    """
    diag = diagnose(isolated_fragment)  # requires AWS credentials — will raise if not configured

    # finding #24: route() now takes DiagnosticianOutput directly — no more
    # hand-copying every field into a separate, duplicate DiagnosticianVerdict
    # shape first.
    decision = route(diag)

    alert_id = None
    if decision.action == BuildAction.FREEZE:
        # only frozen (high-risk) cases need an Attending review record —
        # low-risk log-and-pass and dismissed-false-positive cases aren't
        # persisted for MVP scope
        # finding #28: taxonomy_id/risk_score/citation/plain_english_summary
        # no longer need an `or <default>` fallback here — reaching FREEZE
        # requires matched=True, and DiagnosticianOutput's own validator
        # (finding #46) already guarantees those four are non-None whenever
        # matched=True. remediation_patch is the one field that validator
        # deliberately does NOT enforce (a legitimately optional field), so
        # it keeps its fallback.
        # finding #6 follow-up (independent review, 2026-09-05): the
        # alert_id used to be derived from (repo, pr_number, file,
        # taxonomy_id) alone — too coarse. Two different violations of the
        # same taxonomy_id in the same file collided on the identical id.
        # Passing the isolated fragment text (exactly what Diagnostician
        # actually judged) makes the id specific to THIS violation while a
        # real retry of the same violation still re-derives the identical
        # fragment text and correctly collides/overwrites, preserving
        # finding #6's idempotency guarantee — which also makes SQS's
        # at-least-once delivery safe: a duplicate delivery of the same
        # fragment message is a safe no-op, not a duplicate alert.
        alert = put_alert(
            repo=repo_full_name, pr_number=pr_number, file=file,
            taxonomy_id=diag.taxonomy_id, fragment_text=isolated_fragment, risk_score=diag.risk_score,
            plain_english_summary=diag.plain_english_summary,
            citation=diag.citation, remediation_patch=diag.remediation_patch or "",
        )  # requires dynamodb:* permissions
        alert_id = alert.alert_id

    log.info("fragment result: file=%s action=%s alert_id=%s", file, decision.action.value, alert_id)
    return {
        "file": file,
        "action": decision.action.value,
        "reason": decision.reason,
        "diagnostician": diag.model_dump(),
        "alert_id": alert_id,
    }


def process_pr(repo_full_name: str, pr_number: int, code_diff: str) -> dict:
    """
    Sequential local-fallback path: split into fragments, scan each, and
    call process_fragment() for every escalated one, one after another in
    this single process. Used only when SHIP_FRAGMENT_QUEUE_URL isn't
    configured (local dev / tests) — the real deployed path is
    _dispatch_fragments() below, which sends each escalated fragment to
    SQS as its own independent job instead of looping through them all
    here, specifically to avoid this function's own failure mode: a PR
    with enough escalated fragments can exceed a single Lambda invocation's
    hard 900-second timeout partway through, silently losing whatever
    fragments never got their turn (see SHIP_REVIEW_DISPOSITIONS.md,
    2026-09-05, for the real end-to-end test that proved this).
    """
    fragments = split_diff_into_fragments(code_diff)
    results = []
    log.info("process_pr start: repo=%s pr=%s fragments=%d", repo_full_name, pr_number, len(fragments))

    for frag in fragments:
        screener_result = scan(frag["text"])
        if not screener_result.matched:
            continue  # this fragment passes silently, not worth a result entry

        try:
            isolated = isolate_fragment(frag["text"], screener_result.matched_terms)
            results.append(process_fragment(repo_full_name, pr_number, frag["file"], isolated))
        except Exception as e:
            # finding #8: a single fragment's failure (a transient AWS
            # hiccup, a schema-validation error from a malformed model
            # response) used to silently abort every fragment after it in
            # the same PR, since nothing caught it. Record the failure and
            # keep going — a partial result is far better than a silent gap.
            log.exception("fragment failed: file=%s repo=%s pr=%s", frag["file"], repo_full_name, pr_number)
            results.append({
                "file": frag["file"],
                "action": "error",
                "reason": f"{type(e).__name__}: {e}",
                "diagnostician": None,
                "alert_id": None,
            })

    # finding #26: any_frozen used to be a hand-toggled boolean set inside
    # the loop, restating what `results` already encodes — derived here
    # instead, one fewer piece of mutable state to keep in sync.
    any_frozen = any(r["action"] == "freeze" for r in results)
    log.info("process_pr done: repo=%s pr=%s any_frozen=%s escalated=%d", repo_full_name, pr_number, any_frozen, len(results))
    return {
        "action": "freeze" if any_frozen else "pass",
        "fragments_scanned": len(fragments),
        "fragments_escalated": len(results),
        "results": results,
    }


def _dispatch_fragments(repo_full_name: str, pr_number: int, code_diff: str) -> dict:
    """
    Splits the diff, screens each fragment, and sends one SQS message per
    escalated fragment to the fragment-processing queue — each fragment
    becomes its own independent, retryable job instead of being processed
    sequentially inside one Lambda invocation bounded by Lambda's hard
    900-second ceiling. Replaces the old single-Lambda-self-invoke design
    (see this module's docstring for the full 2026-09-05 architecture
    change and why: a live end-to-end test proved the old design could
    silently fail to finish a PR with 3+ escalated fragments).

    Falls back to process_pr()'s synchronous loop if no queue is
    configured (SHIP_FRAGMENT_QUEUE_URL unset) — local dev and the test
    suite keep working with zero AWS dependency, same convenience the old
    LAMBDA_FUNCTION_NAME-unset fallback provided.
    """
    if not FRAGMENT_QUEUE_URL:
        return process_pr(repo_full_name, pr_number, code_diff)

    # Splitting + per-fragment screening now happens here instead of inside
    # process_pr() — finding #19, the old whole-diff-then-per-fragment
    # double scan, is resolved as a natural side effect of needing to know
    # what to actually enqueue, not as separate extra work.
    fragments = split_diff_into_fragments(code_diff)
    escalated = []
    for frag in fragments:
        screener_result = scan(frag["text"])
        if not screener_result.matched:
            continue
        isolated = isolate_fragment(frag["text"], screener_result.matched_terms)
        escalated.append({"file": frag["file"], "isolated_fragment": isolated})

    if not escalated:
        return {"action": "pass", "reason": "Screener found no trigger matches on a per-fragment scan."}

    import boto3

    client = boto3.client("sqs")
    sent = 0
    for frag in escalated:
        payload_bytes = json.dumps({
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "file": frag["file"],
            "isolated_fragment": frag["isolated_fragment"],
        }).encode("utf-8")

        # finding #61 (re-applied per-message rather than per-whole-diff):
        # SQS's real message-size cap is 256KB — an uncaught ClientError
        # here would surface as an unhandled 500 in the "fast, reliable"
        # leg. Each message now carries only ONE fragment's isolated text
        # rather than the entire diff, so this is far less likely to bite
        # in practice than it was for the old whole-PR payload, but the
        # same defensive check applies.
        if len(payload_bytes) > SQS_MESSAGE_SIZE_LIMIT:
            log.warning("fragment too large to enqueue, skipped: file=%s bytes=%d", frag["file"], len(payload_bytes))
            continue

        client.send_message(QueueUrl=FRAGMENT_QUEUE_URL, MessageBody=payload_bytes.decode("utf-8"))
        sent += 1

    if sent == 0:
        return {"action": "pass", "reason": "Every escalated fragment was too large to enqueue — none analyzed."}
    return {"action": "accepted", "reason": f"Screener flagged {sent} fragment(s); each queued for independent review."}


@app.post("/api/v1/webhook")
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)
    payload = await request.json()

    if is_pr_event(payload):
        repo_full_name, pr_number = extract_pr_ref(payload)
        # finding #7: don't let a payload name an arbitrary repo and spend
        # this service's own GitHub token / Bedrock quota against it.
        if repo_full_name not in ALLOWED_REPOS:
            raise HTTPException(status_code=403, detail=f"Repo {repo_full_name!r} is not in the configured allowlist")
        # finding #62: fetch_pr_diff()'s raise_for_status() was uncaught
        # right here, in the "fast, reliable" leg specifically meant not
        # to crash — a closed/deleted PR, an expired token, or a transient
        # GitHub 5xx turned into an unhandled requests.HTTPError and a bare
        # 500 with no logged context. Log it clearly (same "make failures
        # visible" principle as finding #45's timeout fix) and return a
        # real HTTP error GitHub's own webhook redelivery can react to,
        # instead of an opaque crash.
        try:
            code_diff = fetch_pr_diff(repo_full_name, pr_number)
        except requests.exceptions.RequestException as e:
            log.exception("fetch_pr_diff failed: repo=%s pr=%s", repo_full_name, pr_number)
            raise HTTPException(status_code=502, detail=f"Could not fetch PR diff from GitHub: {e}") from e
    else:
        # Simplified {"code_diff": "..."} body, for local testing without a
        # real GitHub webhook payload.
        repo_full_name, pr_number = "local-test", 0
        code_diff = payload.get("code_diff", "")

    if not code_diff:
        return {"action": "pass", "reason": "No diff content in the payload."}

    # Cheap whole-diff precheck (pure regex, no network) — if literally
    # nothing matches anywhere, skip fragment dispatch entirely rather than
    # spending the work to split and per-fragment-scan for nothing.
    if not scan(code_diff).matched:
        return {"action": "pass", "reason": "Screener found no trigger matches."}

    # finding #44: this used to call process_pr() synchronously inline for
    # the local-test payload shape specifically — the exact multi-minute
    # operation the async split exists to move off the request-response
    # cycle, reachable by ANY caller who could produce this body shape, not
    # just our own test harness. Both payload shapes now go through the
    # same dispatch; _dispatch_fragments() itself falls back to the
    # sequential process_pr() path only when no fragment queue is
    # configured at all.
    return _dispatch_fragments(repo_full_name, pr_number, code_diff)
