"""
FastAPI webhook ingestion — wires Screener -> Diagnostician -> Triage -> alert_store.

Deliberately kept as a plain FastAPI service rather than API Gateway (see
ship_roadmap.md's AWS-native stack section for why FastAPI over Lambda's
usual API Gateway pairing) — this same app is wrapped for Lambda via
Mangum (see lambda_handler.py) rather than needing a separate deployment.

Signature verification is real (HMAC-SHA256, tested in
tests/test_webhook_signature.py).

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
"""

import hashlib
import hmac
import json
import os
import time

from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.diagnostician import diagnose
from src.agents.screener import isolate_fragment, scan, split_diff_into_fragments
from src.agents.triage import BuildAction, DiagnosticianVerdict, route
from src.api.dashboard import router as dashboard_router
from src.api.github_client import extract_pr_ref, fetch_pr_diff, is_pr_event
from src.storage.alert_store import put_alert

app = FastAPI(title="SHIP")
app.include_router(dashboard_router)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
LAMBDA_FUNCTION_NAME = os.environ.get("SHIP_LAMBDA_FUNCTION_NAME", "")
ASYNC_MARKER = "ship_internal_action"


def _verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    if not GITHUB_WEBHOOK_SECRET:
        # not yet provisioned — allow through in dev, but this MUST be set
        # before pointing this at a real GitHub webhook (see roadmap "Still open")
        return
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
    each, persist an alert per fragment that freezes. Called either inline
    (local-test payloads, where there's no GitHub delivery deadline to
    respect) or from the async self-invocation path for real PR events.
    """
    fragments = split_diff_into_fragments(code_diff)
    results = []
    any_frozen = False
    first_call = True

    for frag in fragments:
        screener_result = scan(frag["text"])
        if not screener_result.matched:
            continue  # this fragment passes silently, not worth a result entry

        # Confirmed real limit (2026-09-04): Bedrock's cross-region requests-
        # per-minute quota for our model is 10/min — a densely-flagged PR can
        # legitimately exceed that if every escalated fragment fires back to
        # back. Self-service quota increases aren't granted for this account
        # (same "not really a raisable quota" pattern as the earlier Bedrock
        # access saga - see ship_roadmap.md), so pace our own calls instead
        # of relying on a limit that may not move. ~7s keeps us safely under
        # 10/min even counting a fragment's own internal retries.
        if not first_call:
            time.sleep(7)
        first_call = False

        isolated = isolate_fragment(frag["text"], screener_result.matched_terms)
        diag = diagnose(isolated)  # requires AWS credentials — will raise if not configured

        verdict = DiagnosticianVerdict(
            matched=diag.matched,
            taxonomy_id=diag.taxonomy_id,
            risk_score=diag.risk_score,
            plain_english_summary=diag.plain_english_summary,
            citation=diag.citation,
            remediation_patch=diag.remediation_patch,
        )
        decision = route(verdict)

        alert_id = None
        if decision.action == BuildAction.FREEZE:
            any_frozen = True
            # only frozen (high-risk) cases need an Attending review record —
            # low-risk log-and-pass and dismissed-false-positive cases aren't
            # persisted for MVP scope
            alert = put_alert(
                repo=repo_full_name, pr_number=pr_number, taxonomy_id=diag.taxonomy_id or "",
                risk_score=diag.risk_score or 0.0, plain_english_summary=diag.plain_english_summary or "",
                citation=diag.citation or "", remediation_patch=diag.remediation_patch or "",
            )  # requires dynamodb:* permissions
            alert_id = alert.alert_id

        results.append({
            "file": frag["file"],
            "action": decision.action.value,
            "reason": decision.reason,
            "diagnostician": diag.model_dump(),
            "alert_id": alert_id,
        })

    return {
        "action": "freeze" if any_frozen else "pass",
        "fragments_scanned": len(fragments),
        "fragments_escalated": len(results),
        "results": results,
    }


def _invoke_async_processing(repo_full_name: str, pr_number: int, code_diff: str) -> None:
    """
    Fires process_pr() as a separate, fire-and-forget Lambda invocation
    (InvocationType="Event") so the webhook handler can return to GitHub
    immediately instead of making it wait for the full analysis. Requires
    lambda:InvokeFunction on the function's own execution role (self-invoke)
    and SHIP_LAMBDA_FUNCTION_NAME set — falls back to processing inline if
    neither is configured (e.g. local dev without a real Lambda deployment).
    """
    if not LAMBDA_FUNCTION_NAME:
        process_pr(repo_full_name, pr_number, code_diff)
        return

    import boto3

    client = boto3.client("lambda")
    client.invoke(
        FunctionName=LAMBDA_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({
            ASYNC_MARKER: "process_pr",
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "code_diff": code_diff,
        }).encode("utf-8"),
    )


@app.post("/api/v1/webhook")
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)
    payload = await request.json()

    if is_pr_event(payload):
        repo_full_name, pr_number = extract_pr_ref(payload)
        code_diff = fetch_pr_diff(repo_full_name, pr_number)

        if not code_diff:
            return {"action": "pass", "reason": "No diff content for this PR."}

        # Cheap whole-diff precheck (pure regex, no network) — if literally
        # nothing matches anywhere, skip the async invocation entirely
        # rather than spending a Lambda invoke + cold start for nothing.
        if not scan(code_diff).matched:
            return {"action": "pass", "reason": "Screener found no trigger matches."}

        _invoke_async_processing(repo_full_name, pr_number, code_diff)
        return {"action": "accepted", "reason": "Screener flagged at least one match; analyzing asynchronously."}

    # Simplified {"code_diff": "..."} body, for local testing without a real
    # GitHub webhook payload — no GitHub delivery deadline to respect here,
    # so just process inline and return the real result directly.
    code_diff = payload.get("code_diff", "")
    if not code_diff:
        return {"action": "pass", "reason": "No code_diff in payload and not a recognized PR event."}
    return process_pr("local-test", 0, code_diff)
