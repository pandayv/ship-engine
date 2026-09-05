"""
FastAPI webhook ingestion — wires Screener -> Diagnostician -> Triage -> alert_store.

Deliberately kept as a plain FastAPI service rather than API Gateway (see
ship_roadmap.md's AWS-native stack section for why FastAPI over Lambda's
usual API Gateway pairing) — this same app is wrapped for Lambda via
Mangum (see lambda_handler.py) rather than needing a separate deployment.

IMPORTANT architecture note (found + fixed 2026-09-04): GitHub's webhook
delivery does NOT wait several minutes for a response — a real PR with
several escalated fragments, each needing a real Bedrock/AgentCore call
(now additionally paced to respect a confirmed 10/min rate limit), can
easily take 3-6+ minutes total. Doing that synchronously inside the
webhook handler meant GitHub marked the delivery as failed/timed-out even
though the pipeline completed correctly (proven via DynamoDB writes) --
correct results, but looks broken from GitHub's side, which matters for a
live demo's reliability.

Fix: /api/v1/webhook does the CHEAP work only (signature check, parse
payload, a whole-diff Screener precheck) and returns immediately. If
anything might be worth escalating, it fires the actual multi-fragment
Diagnostician processing as a separate ASYNC Lambda self-invocation
(InvocationType="Event") rather than awaiting it inline — see
_invoke_async_processing() and process_pr() below. lambda_handler.py
detects that self-invocation's marker and routes straight to process_pr(),
bypassing Mangum/FastAPI entirely for that path.

2026-09-05 architecture-review fix pass (see SHIP_REVIEW_DISPOSITIONS.md
for the full plain-English writeup of every finding):
- #1: signature verification now fails CLOSED by default (was fail-open
  when GITHUB_WEBHOOK_SECRET was unset) — set SHIP_ALLOW_UNSIGNED=true to
  explicitly opt into the old dev-convenience behavior.
- #44: the local-test `{"code_diff": ...}` payload path no longer gets
  special-cased synchronous processing inline in the request — it goes
  through the same async dispatch as a real PR event, closing the same
  GitHub-timeout-shaped failure mode for any caller producing that shape.
- #7: added a minimal repo allowlist (SHIP_ALLOWED_REPOS) — a payload
  can't make this service spend its GitHub token / Bedrock quota against
  an arbitrary repo of the caller's choosing.
- #8: a single fragment's exception no longer aborts every fragment after
  it in the same PR — caught and recorded per-fragment instead.
- #60: file path now threaded through into put_alert() so a multi-file
  PR's alerts are traceable back to which file triggered each one.
- #61: async invoke payload size is checked against Lambda's 256KB Event
  limit before attempting the call, instead of letting an oversized PR
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
logging.basicConfig(level=logging.INFO)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOW_UNSIGNED = os.environ.get("SHIP_ALLOW_UNSIGNED", "").lower() == "true"
LAMBDA_FUNCTION_NAME = os.environ.get("SHIP_LAMBDA_FUNCTION_NAME", "")
ASYNC_MARKER = "ship_internal_action"
ASYNC_INVOKE_PAYLOAD_LIMIT = 250_000  # bytes; AWS's real cap is 256KB, small margin for JSON overhead
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


def process_pr(repo_full_name: str, pr_number: int, code_diff: str) -> dict:
    """
    The actual (slow) analysis: split into fragments, scan/diagnose/route
    each, persist an alert per fragment that freezes. Always reached via
    the async self-invocation path for real deployments — see
    _invoke_async_processing().
    """
    fragments = split_diff_into_fragments(code_diff)
    results = []
    log.info("process_pr start: repo=%s pr=%s fragments=%d", repo_full_name, pr_number, len(fragments))

    for frag in fragments:
        screener_result = scan(frag["text"])
        if not screener_result.matched:
            continue  # this fragment passes silently, not worth a result entry

        # finding #45: pacing against the real Bedrock quota (10/min,
        # cross-region, confirmed 2026-09-04 - see ship_roadmap.md) used to
        # live here as a fixed time.sleep(7) between fragments. That assumed
        # exactly one Bedrock call per fragment, which isn't how a tool-
        # using Diagnostician agent behaves (RAG lookups + completions, any
        # of which can retry) - moved to src/aws/bedrock_session.py, a
        # shared rate limiter at the actual transport boundary every
        # Bedrock call goes through, regardless of how many calls a given
        # fragment ends up making.
        try:
            isolated = isolate_fragment(frag["text"], screener_result.matched_terms)
            diag = diagnose(isolated)  # requires AWS credentials — will raise if not configured

            # finding #24: route() now takes DiagnosticianOutput directly —
            # no more hand-copying every field into a separate, duplicate
            # DiagnosticianVerdict shape first.
            decision = route(diag)

            alert_id = None
            if decision.action == BuildAction.FREEZE:
                # only frozen (high-risk) cases need an Attending review record —
                # low-risk log-and-pass and dismissed-false-positive cases aren't
                # persisted for MVP scope
                # finding #28: taxonomy_id/risk_score/citation/plain_english_summary
                # no longer need an `or <default>` fallback here — reaching
                # FREEZE requires matched=True, and DiagnosticianOutput's own
                # validator (finding #46) already guarantees those four are
                # non-None whenever matched=True. remediation_patch is the
                # one field that validator deliberately does NOT enforce (a
                # legitimately optional field), so it keeps its fallback.
                # finding #6 follow-up (independent review, 2026-09-05):
                # the alert_id used to be derived from (repo, pr_number,
                # file, taxonomy_id) alone — too coarse. Two different
                # violations of the same taxonomy_id in the same file
                # collided on the identical id, and the second put_alert()
                # call would silently overwrite the first's row. Passing
                # the isolated fragment text (exactly what Diagnostician
                # actually judged) makes the id specific to THIS violation
                # while a real retry of the same violation still re-derives
                # the identical fragment text and correctly collides/
                # overwrites, preserving finding #6's idempotency guarantee.
                alert = put_alert(
                    repo=repo_full_name, pr_number=pr_number, file=frag["file"],
                    taxonomy_id=diag.taxonomy_id, fragment_text=isolated, risk_score=diag.risk_score,
                    plain_english_summary=diag.plain_english_summary,
                    citation=diag.citation, remediation_patch=diag.remediation_patch or "",
                )  # requires dynamodb:* permissions
                alert_id = alert.alert_id

            log.info("fragment result: file=%s action=%s alert_id=%s", frag["file"], decision.action.value, alert_id)
            results.append({
                "file": frag["file"],
                "action": decision.action.value,
                "reason": decision.reason,
                "diagnostician": diag.model_dump(),
                "alert_id": alert_id,
            })
        except Exception as e:
            # finding #8: a single fragment's failure (a transient AWS
            # hiccup, a schema-validation error from a malformed model
            # response) used to silently abort every fragment after it in
            # the same PR, since nothing caught it. Record the failure and
            # keep going — a partial result is far better than a silent gap.
            #
            # Live-caught gap (2026-09-05, during finding #45's deployment
            # verification): this except block recorded the failure into
            # `results`, but for the real production path — an async,
            # fire-and-forget Lambda invocation (InvocationType="Event") —
            # nothing ever reads `results` back. A real fragment failure
            # here was completely invisible: the invocation reported
            # success in CloudWatch, took the full multi-minute processing
            # time, and silently produced zero alerts. log.exception()
            # (not just recording into the discarded return value) is what
            # actually makes this debuggable from CloudWatch.
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


def _invoke_async_processing(repo_full_name: str, pr_number: int, code_diff: str) -> dict:
    """
    Fires process_pr() as a separate, fire-and-forget Lambda invocation
    (InvocationType="Event") so the webhook handler can return to GitHub
    immediately instead of making it wait for the full analysis. Requires
    lambda:InvokeFunction on the function's own execution role (self-invoke)
    and SHIP_LAMBDA_FUNCTION_NAME set — falls back to processing inline if
    neither is configured (e.g. local dev without a real Lambda deployment).
    """
    if not LAMBDA_FUNCTION_NAME:
        return process_pr(repo_full_name, pr_number, code_diff)

    payload_bytes = json.dumps({
        ASYNC_MARKER: "process_pr",
        "repo_full_name": repo_full_name,
        "pr_number": pr_number,
        "code_diff": code_diff,
    }).encode("utf-8")

    # finding #61: AWS caps async (InvocationType="Event") payloads at
    # 256KB, well below the 6MB synchronous limit — an uncaught ClientError
    # here used to surface as an unhandled 500 in the "fast, reliable" leg
    # of the handler, for exactly the kind of PR (a vendored file, a
    # generated migration, a lockfile update) the async redesign was built
    # to handle reliably.
    if len(payload_bytes) > ASYNC_INVOKE_PAYLOAD_LIMIT:
        return {"action": "pass", "reason": f"Diff too large to process asynchronously ({len(payload_bytes)} bytes) — skipped rather than risk a delivery failure."}

    import boto3

    client = boto3.client("lambda")
    client.invoke(FunctionName=LAMBDA_FUNCTION_NAME, InvocationType="Event", Payload=payload_bytes)
    return {"action": "accepted", "reason": "Screener flagged at least one match; analyzing asynchronously."}


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
    # nothing matches anywhere, skip the async invocation entirely rather
    # than spending a Lambda invoke + cold start for nothing.
    if not scan(code_diff).matched:
        return {"action": "pass", "reason": "Screener found no trigger matches."}

    # finding #44: this used to call process_pr() synchronously inline for
    # the local-test payload shape specifically — the exact multi-minute
    # operation the async split exists to move off the request-response
    # cycle, reachable by ANY caller who could produce this body shape, not
    # just our own test harness. Both payload shapes now go through the
    # same dispatch; _invoke_async_processing() itself falls back to inline
    # processing only when no real Lambda deployment is configured at all.
    return _invoke_async_processing(repo_full_name, pr_number, code_diff)
