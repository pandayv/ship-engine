"""
Attending — the human-in-the-loop review console. A FastAPI router (mounted
into the main app, not a separate service) rather than a standalone
Streamlit process — keeps the stack to one deployable thing.

The route handlers themselves are fully testable without DynamoDB by
monkeypatching src.storage.alert_store's list_active_alerts/resolve_alert
(see tests/test_dashboard.py) — this file's own logic (rendering, routing,
which action calls which store function) is verified even though a live
DynamoDB round-trip isn't.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.storage import alert_store

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "dashboard_ui" / "templates"))


@router.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request):
    alerts = alert_store.list_active_alerts()  # requires dynamodb:* — untested live
    return templates.TemplateResponse(request, "attending.html", {"alerts": alerts})


@router.post("/dashboard/{alert_id}/approve")
def approve(alert_id: str):
    alert_store.resolve_alert(alert_id, approved=True)
    # TODO (Milestone B follow-up, not yet built): actually apply
    # diag.remediation_patch back to the PR via the GitHub API and unpause
    # the pipeline. Today this only records the human decision.
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/{alert_id}/reject")
def reject(alert_id: str):
    alert_store.resolve_alert(alert_id, approved=False)
    return RedirectResponse(url="/dashboard", status_code=303)
