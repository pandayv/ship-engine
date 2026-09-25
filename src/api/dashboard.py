"""
Gate — the human-in-the-loop review console. A FastAPI router (mounted
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
human review" point with no credential at all. Gated behind a shared-
secret token (SHIP_DASHBOARD_TOKEN).

Session cookie (added 2026-09-10): a bare `?token=` on every link reads as
a raw secret pasted into a URL, not a deployed web app — it leaks into
browser history, referrer headers, and screenshots. A visitor now hits
`/dashboard/login` once (directly, or automatically via a shared link
carrying `?token=`), and an httponly session cookie carries them through
the rest of the console from there. The query parameter still works
exactly as before — this is additive, not a replacement — so an existing
bookmarked link or a script hitting the API directly is unaffected.

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

from fastapi import APIRouter, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from src.api.github_writeback import comment_for_decision, post_pr_comment, sync_pr_check
from src.storage import alert_store, repo_store
from src.storage.status_store import refresh_summary

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "dashboard_ui" / "templates"))

DASHBOARD_TOKEN = os.environ.get("SHIP_DASHBOARD_TOKEN", "")
ALLOW_UNAUTHENTICATED = os.environ.get("SHIP_ALLOW_UNAUTHENTICATED_DASHBOARD", "").lower() == "true"
SESSION_COOKIE = "ship_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # a week — long enough that a judge doesn't re-authenticate mid-review


def _reviewer_name() -> str:
    """Whose name goes on the decision recorded against the PR. A single
    configured reviewer rather than real accounts: the dashboard is gated
    by one shared token, so claiming to know which individual clicked
    would be an attribution this system cannot actually support."""
    return os.environ.get("SHIP_REVIEWER_NAME", "the SHIP reviewer")


def _check_configured() -> None:
    if not DASHBOARD_TOKEN and not ALLOW_UNAUTHENTICATED:
        raise HTTPException(status_code=500, detail="SHIP_DASHBOARD_TOKEN is not configured on the server")


def _authenticated(request: Request, token: str | None) -> bool:
    """True if this request is allowed through, via EITHER an existing
    session cookie or a valid `?token=`. Does not itself set the cookie —
    see _set_session() below — so it stays a pure check, callable from a
    POST action route that never wants to establish a fresh session."""
    if not DASHBOARD_TOKEN:
        return ALLOW_UNAUTHENTICATED  # explicit local-dev opt-out, same pattern as the webhook secret
    cookie_token = request.cookies.get(SESSION_COOKIE)
    if cookie_token and hmac.compare_digest(cookie_token, DASHBOARD_TOKEN):
        return True
    # Independent review (2026-09-05): this was a plain `!=` comparison,
    # unlike the webhook's own HMAC check in the same fix pass, which
    # correctly used hmac.compare_digest. A data-dependent short-circuiting
    # string comparison risks a timing side-channel for a shared-secret
    # token check; compare_digest is the standard fix.
    return bool(token) and hmac.compare_digest(token, DASHBOARD_TOKEN)


def _set_session(resp: Response, request: Request, token: str | None) -> None:
    """Establishes the session cookie when a request arrived with a fresh,
    valid `?token=` and doesn't already carry it — so a link shared once
    (`/dashboard?token=...`) logs a visitor in for the rest of their visit
    without the token reappearing in every subsequent URL. Cookies set on
    an injected `Response` are NOT copied onto a *different* Response
    object a route returns (a FastAPI/Starlette gotcha) — every route
    below calls this on the actual object it's about to return, not on a
    separate injected one."""
    if not DASHBOARD_TOKEN or not token:
        return
    if request.cookies.get(SESSION_COOKIE) == token:
        return  # already set to this value
    if hmac.compare_digest(token, DASHBOARD_TOKEN):
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=True, max_age=SESSION_MAX_AGE)


@router.get("/dashboard/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/dashboard"):
    return templates.TemplateResponse(request, "gate_login.html", {"next": next, "error": None})


@router.post("/dashboard/login", response_class=HTMLResponse)
def login_submit(request: Request, token: str = Form(...), next: str = Form(default="/dashboard")):
    _check_configured()
    if not hmac.compare_digest(token, DASHBOARD_TOKEN):
        return templates.TemplateResponse(
            request, "gate_login.html", {"next": next, "error": "That token doesn't match."}, status_code=401,
        )
    resp = RedirectResponse(url=next, status_code=303)
    _set_session(resp, request, token)
    return resp


@router.get("/dashboard/logout")
def logout():
    """Tells THIS browser to forget its session cookie. Does not, and
    cannot, revoke the underlying credential: the cookie holds the actual
    shared token, not a lookup key into a server-side session store, so
    anyone who separately captured that token value still holds a valid
    one after this call. The only way to actually revoke access is
    rotating SHIP_DASHBOARD_TOKEN itself. Worth being explicit about this
    rather than letting "logout" imply a guarantee it doesn't make."""
    resp = RedirectResponse(url="/dashboard/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
def view_dashboard(request: Request, token: str | None = Query(default=None)):
    _check_configured()
    if not _authenticated(request, token):
        return RedirectResponse(url="/dashboard/login?next=/dashboard", status_code=303)
    alerts = alert_store.list_active_alerts()
    resp = templates.TemplateResponse(request, "gate.html", {"alerts": alerts})
    _set_session(resp, request, token)
    return resp


def _resolve(alert_id: str, request: Request, token: str | None, approved: bool, reason: str) -> RedirectResponse:
    # finding #37: approve()/reject() used to be a near-duplicate pair
    # differing only in this boolean — factored out so the write-back to
    # GitHub below only needed writing once, not twice.
    if not _authenticated(request, token):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    reason = (reason or "").strip()
    if not reason:
        # The reason IS the audit record — a decision with no stated basis
        # is not much better than no decision having been recorded at all.
        raise HTTPException(status_code=400, detail="A reason is required — this is the audit record.")

    alert = alert_store.get_alert(alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert_id {alert_id!r} not found")

    try:
        alert_store.resolve_alert(alert_id, approved=approved, reason=reason, resolved_by=_reviewer_name())
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
    # A resolved finding changes what Herald should say next time it is
    # asked, so the summary is refreshed here too.
    refresh_summary()

    resp = RedirectResponse(url="/dashboard", status_code=303)
    _set_session(resp, request, token)
    return resp


@router.post("/dashboard/{alert_id}/approve")
def approve(alert_id: str, request: Request, token: str | None = Query(default=None), reason: str = Form(default="")):
    return _resolve(alert_id, request, token, approved=True, reason=reason)


@router.post("/dashboard/{alert_id}/reject")
def reject(alert_id: str, request: Request, token: str | None = Query(default=None), reason: str = Form(default="")):
    return _resolve(alert_id, request, token, approved=False, reason=reason)


@router.get("/dashboard/history", response_class=HTMLResponse)
def view_history(request: Request, token: str | None = Query(default=None)):
    """The dispositions view: every alert a human has already resolved,
    most recent first, with the reason they gave — the same audit record
    that's posted to the PR, readable here without leaving the dashboard
    or hunting through PR comments across repos."""
    _check_configured()
    if not _authenticated(request, token):
        return RedirectResponse(url="/dashboard/login?next=/dashboard/history", status_code=303)
    alerts = alert_store.list_resolved_alerts()
    resp = templates.TemplateResponse(request, "gate_history.html", {"alerts": alerts})
    _set_session(resp, request, token)
    return resp


@router.get("/dashboard/repos", response_class=HTMLResponse)
def view_repos(request: Request, token: str | None = Query(default=None)):
    """Which repositories SHIP is watching — see src/storage/repo_store.py
    for why this replaced a static SHIP_ALLOWED_REPOS env var: connecting a
    repo used to be a redeploy, capped by Lambda's 4KB env var limit."""
    _check_configured()
    if not _authenticated(request, token):
        return RedirectResponse(url="/dashboard/login?next=/dashboard/repos", status_code=303)
    repos = repo_store.list_repos()
    resp = templates.TemplateResponse(request, "gate_repos.html", {"repos": repos})
    _set_session(resp, request, token)
    return resp


@router.post("/dashboard/repos/connect")
def connect_repo(request: Request, token: str | None = Query(default=None), repo_full_name: str = Form(...)):
    if not _authenticated(request, token):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    repo_full_name = repo_full_name.strip()
    if "/" not in repo_full_name or repo_full_name.startswith("/") or repo_full_name.endswith("/"):
        raise HTTPException(status_code=400, detail="Expected 'owner/repo'")
    repo_store.connect_repo(repo_full_name, connected_by=_reviewer_name())
    resp = RedirectResponse(url="/dashboard/repos", status_code=303)
    _set_session(resp, request, token)
    return resp


@router.post("/dashboard/repos/{repo_full_name:path}/disconnect")
def disconnect_repo(repo_full_name: str, request: Request, token: str | None = Query(default=None)):
    if not _authenticated(request, token):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    repo_store.disconnect_repo(repo_full_name)
    resp = RedirectResponse(url="/dashboard/repos", status_code=303)
    _set_session(resp, request, token)
    return resp
