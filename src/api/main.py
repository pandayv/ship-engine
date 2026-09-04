"""
FastAPI webhook ingestion — wires Screener -> Diagnostician -> Triage.

Deliberately kept as a plain FastAPI service rather than Lambda+API Gateway
(see ship_roadmap.md's AWS-native stack section for why). The webhook
signature verification below is a placeholder (GITHUB_WEBHOOK_SECRET not yet
provisioned) — real verification needs to be wired in before this is pointed
at a live GitHub webhook. Diagnostician calls require AWS credentials that
aren't configured in this environment; that part is untested end-to-end.
"""

import hashlib
import hmac
import os

from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.diagnostician import diagnose
from src.agents.screener import isolate_fragment, scan
from src.agents.triage import DiagnosticianVerdict, route

app = FastAPI(title="SHIP webhook ingestion")

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

    # Real GitHub PR payloads carry the diff via a separate API call (the
    # webhook event itself doesn't include the full patch text) — this
    # extraction is a placeholder pending that wiring; for now, accept a
    # simplified {"code_diff": "..."} body directly for local testing.
    code_diff = payload.get("code_diff", "")
    if not code_diff:
        return {"action": "pass", "reason": "No code_diff in payload (real PR-diff fetch not yet wired)"}

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

    # TODO: persist to DynamoDB for Attending to read (not yet built —
    # Milestone D). For now, decision is returned but not stored anywhere.
    return {"action": decision.action.value, "reason": decision.reason, "diagnostician": diag.model_dump()}
