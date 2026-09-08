"""
Attending — the human-in-the-loop review console. A FastAPI router (mounted
into the main app, not a separate service) rather than a standalone
Streamlit process — keeps the stack to one deployable thing.

The route handlers themselves are fully testable without DynamoDB by
monkeypatching src.storage.alert_store's list_active_alerts/resolve_alert
(see tests/test_dashboard.py) — this file's own logic (rendering, routing,
which action calls which store function) is verified even though a live
DynamoDB round-trip isn't.

Auth (added 2026-09-05, review finding #2): all three routes previously had
zero authentication — anyone who could reach the URL could clear a frozen
high-risk alert with a bare curl, defeating the entire "freeze pending
human review" point with no credential at all. Now gated behind a shared-
secret token (SHIP_DASHBOARD_TOKEN), passed as a `token` query parameter so
it works uniformly for a browser GET and for the HTML forms' POST actions
without needing custom headers.

Real bug an independent review caught (2026-09-05): the local-dev opt-out
used to read the exact same SHIP_ALLOW_UNSIGNED env var the webhook uses
to bypass its own, unrelated HMAC signature check. Setting that flag to
test webhook delivery locally silently disabled dashboard auth too, with
no indication that happened as a side effect of an unrelated setting.
Given its own dedicated flag (SHIP_ALLOW_UNAUTHENTICATED_DASHBOARD) so the
two security controls can only ever be bypassed independently, on purpose.
"""

import hmac
import os
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.api.github_writeback import comment_for_decision, post_pr_comment, sync_pr_check
from src.storage import alert_store

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "dashboard_ui" / "templates"))

DASHBOARD_TOKEN = os.environ.get("SHIP_DASHBOARD_TOKEN", "")
ALLOW_UNAUTHENTICATED = os.environ.get("SHIP_ALLOW_UNAUTHENTICATED_DASHBOARD", "").lower() == "true"


def _reviewer_name() -> str:
    """Whose name goes on the decision recorded against the PR. A single
    configured reviewer rather than real accounts: the dashboard is gated
    by one shared token, so claiming to know which individual clicked
    would be an attribution this system cannot actually support."""
    return os.environ.get("SHIP_REVIEWER_NAME", "the SHIP reviewer")


def _require_token(token: str | None) -> None:
    if not DASHBOARD_TOKEN:
        if ALLOW_UNAUTHENTICATED:
            return  # explicit local-dev opt-out, same pattern as the webhook secret
        raise HTTPException(status_code=500, detail="SHIP_DASHBOARD_TOKEN is not configured on the server")
    # Independent review (2026-09-05): this was a plain `!=` comparison,
    # unlike the webhook's own HMAC check in the same fix pass, which
    # correctly used hmac.compare_digest. A data-dependent short-circuiting
    # string comparison risks a timing side-channel for a shared-secret
    # token check; compare_digest is the standard fix.
    if not token or not hmac.compare_digest(token, DASHBOARD_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid token")


@router.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request, token: str | None = Query(default=None)):
    _require_token(token)
    alerts = alert_store.list_active_alerts()
    return templates.TemplateResponse(request, "attending.html", {"alerts": alerts, "token": token})


def _resolve(alert_id: str, token: str | None, approved: bool, reason: str) -> RedirectResponse:
    # finding #37: approve()/reject() used to be a near-duplicate pair
    # differing only in this boolean — factored out so the write-back to
    # GitHub below only needed writing once, not twice.
    _require_token(token)

    reason = (reason or "").strip()
    if not reason:
        # The reason IS the audit record — a decision with no stated basis
        # is not much better than no decision having been recorded at all.
        raise HTTPException(status_code=400, detail="A reason is required — this is the audit record.")

    alert = alert_store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert_id {alert_id!r} not found")

    try:
        alert_store.resolve_alert(alert_id, approved=approved)
    except alert_store.AlertNotFoundOrAlreadyResolved as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    # Only after the authoritative state change lands. Both calls fail soft
    # (see src/api/github_writeback.py): if GitHub is briefly unreachable
    # the human's decision still stands and is still recorded — the check
    # converges on the next sync rather than the decision being lost.
    post_pr_comment(
        alert.repo, alert.pr_number,
        comment_for_decision(alert, accepted=approved, reason=reason, who=_reviewer_name()),
    )
    sync_pr_check(alert.repo, alert.pr_number, alert.head_sha)

    return RedirectResponse(url=f"/dashboard?token={token}", status_code=303)


@router.post("/dashboard/{alert_id}/approve")
def approve(alert_id: str, token: str | None = Query(default=None), reason: str = Form(default="")):
    return _resolve(alert_id, token, approved=True, reason=reason)


@router.post("/dashboard/{alert_id}/reject")
def reject(alert_id: str, token: str | None = Query(default=None), reason: str = Form(default="")):
    return _resolve(alert_id, token, approved=False, reason=reason)
