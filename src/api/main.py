"""
FastAPI webhook ingestion — wires Screener -> Diagnostician -> Triage -> alert_store.

Deliberately kept as a plain FastAPI service rather than Lambda+API Gateway
(see ship_roadmap.md's AWS-native stack section for why).

Signature verification is real (HMAC-SHA256, tested in
tests/test_webhook_signature.py) but GITHUB_WEBHOOK_SECRET isn't provisioned
yet — verification silently no-ops until that env var is set, which MUST
happen before this is pointed at a live GitHub webhook.

PR-diff fetching is real and tested against the live PR #1 fixture
(src/api/github_client.py). Diagnostician and alert_store calls need AWS
credentials/permissions this environment doesn't fully have yet (Bedrock
calls are blocked on a daily token quota as of this writing; DynamoDB
permissions haven't been added to ship-agent's policy at all) — those two
legs of the pipeline are code-complete but not live-tested end-to-end.
"""

import hashlib
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.diagnostician import diagnose
from src.agents.screener import isolate_fragment, scan
from src.agents.triage import BuildAction, DiagnosticianVerdict, route
from src.api.dashboard import router as dashboard_router
from src.api.github_client import extract_pr_ref, fetch_pr_diff, is_pr_event
from src.storage.alert_store import put_alert

app = FastAPI(title="SHIP")
app.include_router(dashboard_router)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")


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


@app.post("/api/v1/webhook")
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)
    payload = await request.json()

    if is_pr_event(payload):
        repo_full_name, pr_number = extract_pr_ref(payload)
        code_diff = fetch_pr_diff(repo_full_name, pr_number)
    else:
        # simplified {"code_diff": "..."} body, for local testing without a
        # real GitHub webhook payload — no real PR to link an alert back to
        repo_full_name, pr_number = "local-test", 0
        code_diff = payload.get("code_diff", "")

    if not code_diff:
        return {"action": "pass", "reason": "No code_diff in payload and not a recognized PR event."}

    screener_result = scan(code_diff)
    if not screener_result.matched:
        return {"action": "pass", "reason": "Screener found no trigger matches.", "screener": screener_result.to_dict()}

    fragment = isolate_fragment(code_diff, screener_result.matched_terms)
    diag = diagnose(fragment)  # requires AWS credentials — will raise if not configured

    verdict = DiagnosticianVerdict(
        matched=diag.matched,
        taxonomy_id=diag.taxonomy_id,
        risk_score=diag.risk_score,
        plain_english_summary=diag.plain_english_summary,
        citation=diag.citation,
        remediation_patch=diag.remediation_patch,
    )
    decision = route(verdict)

    alert = None
    if decision.action == BuildAction.FREEZE:
        # only frozen (high-risk) cases need an Attending review record —
        # low-risk log-and-pass cases aren't persisted for MVP scope
        alert = put_alert(
            repo=repo_full_name, pr_number=pr_number, taxonomy_id=diag.taxonomy_id or "",
            risk_score=diag.risk_score or 0.0, plain_english_summary=diag.plain_english_summary or "",
            citation=diag.citation or "", remediation_patch=diag.remediation_patch or "",
        )  # requires dynamodb:* permissions — untested until ship-agent's policy grants them

    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "diagnostician": diag.model_dump(),
        "alert_id": alert.alert_id if alert else None,
    }
