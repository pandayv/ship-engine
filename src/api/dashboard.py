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
without needing custom headers. Same escape-hatch pattern as the webhook's
SHIP_ALLOW_UNSIGNED flag, for local dev without the env var set.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.storage import alert_store

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "dashboard_ui" / "templates"))

DASHBOARD_TOKEN = os.environ.get("SHIP_DASHBOARD_TOKEN", "")
ALLOW_UNAUTHENTICATED = os.environ.get("SHIP_ALLOW_UNSIGNED", "").lower() == "true"


def _require_token(token: str | None) -> None:
    if not DASHBOARD_TOKEN:
        if ALLOW_UNAUTHENTICATED:
            return  # explicit local-dev opt-out, same pattern as the webhook secret
        raise HTTPException(status_code=500, detail="SHIP_DASHBOARD_TOKEN is not configured on the server")
    if token != DASHBOARD_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid token")


@router.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request, token: str | None = Query(default=None)):
    _require_token(token)
    alerts = alert_store.list_active_alerts()
    return templates.TemplateResponse(request, "attending.html", {"alerts": alerts, "token": token})


@router.post("/dashboard/{alert_id}/approve")
def approve(alert_id: str, token: str | None = Query(default=None)):
    _require_token(token)
    try:
        alert_store.resolve_alert(alert_id, approved=True)
    except alert_store.AlertNotFoundOrAlreadyResolved as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    # TODO (Milestone B follow-up, not yet built): actually apply
    # diag.remediation_patch back to the PR via the GitHub API and unpause
    # the pipeline. Today this only records the human decision.
    return RedirectResponse(url=f"/dashboard?token={token}", status_code=303)


@router.post("/dashboard/{alert_id}/reject")
def reject(alert_id: str, token: str | None = Query(default=None)):
    _require_token(token)
    try:
        alert_store.resolve_alert(alert_id, approved=False)
    except alert_store.AlertNotFoundOrAlreadyResolved as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return RedirectResponse(url=f"/dashboard?token={token}", status_code=303)
