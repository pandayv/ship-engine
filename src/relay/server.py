"""
Relay — SHIP's MCP server, the layer that lets an assistant ask SHIP
questions without a human opening a browser.

Relay carries no judgment. Detector already reasoned over the code,
Triage already decided whether it blocks, Gate already recorded whatever
a human concluded. Relay only reads that and routes it to a surface.
That is a design principle, and Alexa+'s 500 ms round-trip budget also
makes it the only possible shape — a diagnosis takes ~3 s, ten times the
whole budget.

WHAT VOICE CANNOT DO, AND WHY THAT IS THE POINT

Voice cannot decide a compliance finding BLIND. It can confirm a decision, once a
person has actually looked at the finding on a screen and given a reason.

The first version of this rule was simpler and wrong: no tool here would
ever accept a risk by voice, full stop. Revisited directly (2026-09-28) —
the actual failure mode SHIP exists to catch isn't "a decision happened
over voice," it's "a decision happened without a human genuinely engaging
with what they were deciding." Those aren't the same thing. A person who
asked to see the findings, had them rendered on a real screen, and then
says "override it, here's why" has done exactly what human oversight
requires. Refusing that and forcing a trip to a web form adds friction
without adding safety. What the ORIGINAL refusal correctly blocks, and
what still must never work, is "ignore it, go ahead" with no review at
all — that is the actual TLGP-002 shape (an AI-mediated decision with no
human checkpoint), not "the confirmation channel was a microphone."

So the decision tools check one real, server-side fact before
doing anything: was a screen tool (blocked_pull_requests / finding_detail)
called for THIS alert_id recently — see src/storage/session_store.py. No:
refused, exactly as before, redirected to Gate. Yes, and a reason was
given: performed for real, written to ship-alerts with that reason as the
record, same durability Gate's own resolve gives it. The reason is still
required either way — dropping that would trade a real audit trail for a
bare yes/no flag, which is the one piece of the original design worth
keeping regardless of channel.

The same two facts gate the batch path (stage_decisions, then proceed), checked
again at commit time. Decisions name a finding by its alert_id, never by a live
position: the number a person speaks is the number the screen showed, and findings
that arrive or get decided between the screen and the sentence would otherwise shift
"the second one" onto a different finding. A decision is the word "allow" or "block",
never a boolean, and anything else is refused rather than guessed at. Allow accepts the
risk and the finding stops blocking; block says the finding is real, so the PR stays
blocked until the code changes (see src/api/github_writeback.py).

That check is keyed on the alert, by a time window, not on an MCP session
— a real correction, not the original design. The first version keyed on
Mcp-Session-Id, reasoning it was a stable per-conversation identifier.
Live-tested against the actually-deployed system, not assumed correct
from a local check: it never arrived. `create_app()` below (see the
ARCHITECTURE NOTE) builds a brand-new session manager on every single
invocation, so there is no process for a session id to have continuity
WITH — that isn't a bug to fix, it's what SnapStart-per-invocation
structurally requires. See src/storage/session_store.py's own docstring
for the full correction and why "shown in the last ten minutes" is the
honest signal actually available here.

ARCHITECTURE NOTE — why the ASGI app is a FACTORY, not a module-level
singleton. Confirmed by a real, reproduced failure, not assumed: the MCP
Python SDK's Streamable HTTP transport owns a StreamableHTTPSessionManager
whose lifespan can be entered exactly once per instance. Lambda reuses a
warm container's module-level state across invocations by design (that is
the whole point of container reuse), so a session manager built once at
import time gets reused too — and crashes the SECOND request against that
container with "SessionManager .run() can only be called once per
instance," independent of stateful/stateless mode. `create_app()` below
is called fresh on every invocation by relay_lambda_handler.py specifically
to avoid this: a never-run session manager satisfies that constraint by
construction, every time. Verified locally against the real failure mode
(five independent invocations, not one) before relying on this, per this
project's own standard of testing the failure a fix claims to resolve.

The tool functions themselves stay at module level, outside the factory —
they hold no state, do no I/O at import, and registering them onto a
fresh FastMCP instance costs about 2ms, confirmed by measurement.
"""

from __future__ import annotations

import logging
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from src.api.github_writeback import comment_for_decision, post_pr_comment, sync_pr_check
from src.relay.audit import audit
from src.relay.modality import SPEECH_CHAR_BUDGET, Modality, Spoken
from src.relay.push import push_to_device
from src.relay import webpush
from src.storage import push_store
from src.storage.device_store import list_all_connections
from src.relay.readmodel import findings_detail as _findings_detail
from src.relay.readmodel import release_status as _release_status
from src.storage import pending_store, session_store
from src.storage.alert_store import (DECISION_ALLOW, SEVERITY_BLOCKING, AlertNotFoundOrAlreadyResolved,
                                     get_alert, is_decision, list_gate_alerts, resolve_alert, theme_for)
from src.storage.status_store import SummaryUnavailable, refresh_summary

log = logging.getLogger(__name__)

def _console_root(value: str) -> str:
    """The Gate console's address without its /dashboard path, which _console() adds back.
    The webhook and the fragment processor are given the full /dashboard address; accepting
    both here means one value serves all three."""
    value = (value or "").strip().rstrip("/")
    return value[: -len("/dashboard")] if value.endswith("/dashboard") else value


CONSOLE_URL = _console_root(os.environ.get("SHIP_DASHBOARD_URL", ""))

# MCP's Streamable HTTP transport enables DNS-rebinding protection by
# default (mcp.server.transport_security.TransportSecuritySettings,
# enable_dns_rebinding_protection=True with an EMPTY allowed_hosts) — every
# request is rejected with 421 unless its Host header is explicitly
# allowlisted. Confirmed by a real local invoke, not assumed: an
# unconfigured server returned "Invalid Host header" for a completely
# valid request.
#
# allowed_hosts and allowed_origins are DELIBERATELY two separate
# variables, not one shared list — confirmed necessary by a real failure,
# not assumed. Host is the address a request is sent TO (always this
# Lambda's own Function URL hostname, regardless of caller); Origin is the
# page a BROWSER request came FROM (a completely different value once a
# real web client — the Alexa+ simulator page — calls this endpoint via
# fetch()). Treating them as one list worked fine until a cross-origin
# browser call was actually tried: it came back 403 with zero CORS
# headers, which traced to this exact security check correctly rejecting
# an origin that was never allowlisted, because allowed_origins had been
# silently set to the same single value as allowed_hosts.
#
# Both hostnames don't exist until after their respective resources are
# created (the Function URL; the page's real hosting URL), so both are
# read from environment variables set post-deploy — same pattern as
# finding #42's SHIP_AGENTCORE_RUNTIME_ARN fix, never guessed or
# hardcoded. Comma-separated since a real deployment plausibly has more
# than one valid value for either (the raw Function URL during setup vs.
# a custom domain later; a GitHub Pages origin vs. a custom domain for
# the simulator).
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("SHIP_RELAY_ALLOWED_HOSTS", "").split(",") if h.strip()]
ALLOWED_ORIGINS = [h.strip() for h in os.environ.get("SHIP_RELAY_ALLOWED_ORIGINS", "").split(",") if h.strip()]
if not ALLOWED_HOSTS:
    # Local dev / test only. A real deployment MUST set
    # SHIP_RELAY_ALLOWED_HOSTS or every request is rejected at the
    # transport layer before reaching any tool — fails safe, not silently.
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
if not ALLOWED_ORIGINS:
    # Local dev / test only, same reasoning as ALLOWED_HOSTS above.
    ALLOWED_ORIGINS = ["http://localhost", "http://127.0.0.1"]

INSTRUCTIONS = (
    "SHIP reviews pull requests for AI-compliance risk and can block a merge. "
    "Use release_status for any spoken answer — it is one sentence and is the only "
    "tool safe to read aloud. Never read findings, citations or code aloud: call "
    "blocked_pull_requests or finding_detail and render the result on a screen. "
    "To decide findings, each needs to have been shown on a screen recently and a "
    "reason in the person's own words: call stage_decisions with each finding's "
    "alert_id, decision (\"allow\" accepts the risk; \"block\" confirms the finding is "
    "real, so the PR stays blocked until the code changes) and reason, then proceed "
    "once the person confirms. request_risk_acceptance allows one finding in a single "
    "step under the same two conditions; if either isn't met it returns the console to "
    "open instead of performing anything."
)

# A reason is the audit record; it is written to the alert and to the pull request.
# Voice reasons are a sentence. Longer than this is refused rather than cut: a
# shortened reason is no longer the person's reason.
MAX_REASON_CHARS = 500


def _console(path: str = "") -> str:
    return f"{CONSOLE_URL}{path}" if CONSOLE_URL else "the SHIP review console"


# --- tool logic: plain functions, no I/O at import, registered by create_app() ---


def _screen_names() -> list[str]:
    """Every screen that can be reached: a page that is open now, or one registered for
    alerts. Shared by connected_screens() and the status sentence's hint."""
    live = {c.device_name for c in list_all_connections()}
    try:
        registered = push_store.subscribed_device_names()
    except Exception:  # noqa: BLE001 - the live screens still count if the registry cannot be read
        log.exception("could not read the registered screens")
        registered = set()
    return sorted(live | registered)


def _screen_hint(names: list[str] | None) -> str | None:
    """The question that ends a status sentence, naming only what exists. None when
    nothing is connected (there is nothing to offer) or when that could not be read."""
    if not names:
        return None
    short = [n for n in names if len(n) <= 20]
    if len(names) == 1 and short:
        return f"Want it on the {short[0]}?"
    if len(names) == 2 and len(short) == 2:
        return f"Want it on the {short[0]} or the {short[1]}?"
    return "Want it on a screen?"


def release_status() -> str:
    """Whether anything is currently blocking a release, as one short spoken
    sentence. SAFE TO READ ALOUD. Returns counts and which pull request to
    look at first — never a list of findings, never a citation. Use this for
    "is anything blocked", "can we ship", "what's going on with my release".
    If the count cannot be read it says so; it never reports all clear on a failed read."""
    try:
        status = _release_status()
    except SummaryUnavailable:
        audit("release_status", "unavailable", why="summary_unreadable")
        return Spoken("I can't check right now. Open the review console.").to_text()
    hint = None
    if status.total:
        try:
            hint = _screen_hint(_screen_names())
        except Exception:  # noqa: BLE001 - the answer matters more than the offer
            log.exception("could not list the screens for the status hint")
    audit("release_status", "answered", blocking=status.blocking, review=status.review, held=status.held)
    return status.to_spoken(screen_hint=hint).to_text()


def _pending_view(staged) -> dict | None:
    """What a screen shows for a staged decision. A record whose decision cannot be read
    is not shown at all: a screen must never present something proceed would refuse."""
    if not isinstance(staged, dict) or not is_decision(staged.get("decision")):
        return None
    return {"decision": staged["decision"], "reason": staged.get("reason", "")}


def _findings_payload(status) -> dict:
    """Builds the SCREEN-shaped payload both blocked_pull_requests() and
    the staging tools below push — one implementation, so a finding
    numbered "2" means the same thing whether it got there via a tool
    call or a WebSocket push mid-conversation. Each finding carries its
    pending disposition, if any is currently staged for that PR (see
    src/storage/pending_store.py) — "position" is never recomputed from a
    live pending record, it comes from the same stable ordering
    readmodel._group() already produced, pending or not.

    `type` names the message for pages that dispatch on it; `held` lists pull requests
    that a blocked finding still holds, so a screen never says "all clear" about them."""
    pending_by_pr = {
        (pr.repo, pr.pr_number): {d["alert_id"]: d for d in (pending_store.get_pending(pr.repo, pr.pr_number) or [])
                                  if isinstance(d, dict) and "alert_id" in d}
        for pr in status.pull_requests
    }
    return {
        "type": "findings",
        "modality": Modality.SCREEN.value,
        "display_only": True,
        "summary": {"blocking": status.blocking, "flagged": status.review,
                    "pull_requests": len(status.pull_requests), "held": status.held},
        "held": [{"repo": pr.repo, "pr_number": pr.pr_number, "count": pr.held}
                 for pr in [*status.pull_requests, *status.held_only] if pr.held],
        "pull_requests": [
            {
                "repo": pr.repo,
                "pr_number": pr.pr_number,
                "blocking": pr.blocking,
                "flagged": pr.review,
                "findings": [
                    {
                        "position": i,
                        "theme": theme_for(f.taxonomy_id),
                        "alert_id": f.alert_id,
                        "file": f.file,
                        "severity": f.severity,
                        "blocks_merge": f.severity == SEVERITY_BLOCKING,
                        "risk_score": f.risk_score,
                        "what_is_wrong": f.plain_english_summary,
                        "citation": f.citation,
                        "pending": _pending_view(pending_by_pr[(pr.repo, pr.pr_number)].get(f.alert_id)),
                    }
                    for i, f in enumerate(pr.findings, start=1)
                ],
            }
            for pr in status.pull_requests
        ],
        "console_url": _console("/dashboard"),
    }


def _mark_shown(status) -> None:
    """Records every finding in `status` as recently shown, with the number it has on
    that screen. Call this only once a screen has actually received them: the voice
    gate treats "shown" as "a person could have read it"."""
    for pr in status.pull_requests:
        for i, f in enumerate(pr.findings, start=1):
            session_store.mark_shown(f.alert_id, position=i)


def _routing(status) -> list[dict]:
    """Where each open finding sits, so a later spoken decision can point at it
    ("allow the first one on PR 2"). Positions and ids only: no file names,
    citations or code, so it carries nothing that must stay off the speaker.
    The position matches the number the screen shows."""
    return [
        {
            "repo": pr.repo,
            "pr_number": pr.pr_number,
            "findings": [
                {"position": i, "alert_id": f.alert_id, "blocks_merge": f.severity == SEVERITY_BLOCKING}
                for i, f in enumerate(pr.findings, start=1)
            ],
        }
        for pr in status.pull_requests
    ]


def blocked_pull_requests() -> dict:
    """Every pull request with unresolved findings, with counts and the
    findings themselves. SCREEN ONLY — this is a list and must be displayed,
    never spoken. Use after release_status when the person asks to see detail.
    Each finding carries its alert_id and the number shown on screen.

    Calling this records every returned alert as recently shown — see
    request_risk_acceptance and src/storage/session_store.py. That is a
    side effect of what this tool already does (surfacing the findings on
    a screen), not a new responsibility."""
    status = _findings_detail()
    _mark_shown(status)
    return _findings_payload(status)


def finding_detail(alert_id: str) -> dict:
    """Full detail for one finding: what is wrong, the regulation it breaks,
    and the suggested remediation. SCREEN ONLY — contains a citation and may
    contain code, neither of which should ever be read aloud.

    Calling this records this alert as recently shown — see
    request_risk_acceptance and src/storage/session_store.py."""
    alert = get_alert(alert_id)
    if alert is None:
        return {"modality": Modality.SCREEN.value, "found": False, "alert_id": alert_id}
    session_store.mark_shown(alert_id)
    return {
        "modality": Modality.SCREEN.value,
        "display_only": True,
        "found": True,
        "alert_id": alert.alert_id,
        "repo": alert.repo,
        "pr_number": alert.pr_number,
        "file": alert.file,
        "severity": alert.severity,
        "blocks_merge": alert.severity == SEVERITY_BLOCKING,
        "risk_score": alert.risk_score,
        "what_is_wrong": alert.plain_english_summary,
        "citation": alert.citation,
        "suggested_remediation": alert.remediation_patch,
        "console_url": _console("/dashboard"),
    }


def _resolve_one(alert, decision: str, reason: str) -> None:
    """Shared by request_risk_acceptance and proceed() — the one place a
    voice-driven resolution is recorded and announced on the PR, so both the
    single-finding and batch paths write back the way Gate's web console does,
    not two implementations that could quietly drift. Allow accepts the risk;
    block keeps the PR blocked (see src/api/github_writeback.py).

    This records the decision and comments. The commit status and the summary are
    refreshed by _write_back(), once per request, after every decision in the request
    has landed. The comment fails soft, same as the web path — the decision stands
    either way. Raises AlertNotFoundOrAlreadyResolved if someone decided it first."""
    resolve_alert(alert.alert_id, decision, reason=reason, resolved_by="voice")
    post_pr_comment(
        alert.repo, alert.pr_number,
        comment_for_decision(alert, decision, reason=reason, who="voice"),
    )


def _write_back(decided: list):
    """After decisions landed: ONE read of everything that can block a merge, shared by
    the commit status of each affected PR and the release summary Relay speaks. Returns
    that read (None if it failed) so a following screen push can reuse it instead of
    scanning again. Per request only, never cached: it is a gate decision."""
    try:
        gate = list_gate_alerts()
    except Exception:  # noqa: BLE001 - each callee reads for itself when given None
        log.exception("could not read the gate alerts after a decision")
        gate = None
    for repo, pr, sha in sorted({(a.repo, a.pr_number, a.head_sha) for a in decided}):
        sync_pr_check(repo, pr, sha, alerts=gate)
    refresh_summary(gate)
    return gate


def _refusal(tool: str, why: str, spoken: str, repo: str | None = None, pr_number: int | None = None,
             alert_ids=None, **extra) -> dict:
    """One shape for 'did nothing, here is the honest reason'. Builds the spoken text
    first, so an over-long sentence fails before anything has happened."""
    audit(tool, "refused", repo, pr_number, alert_ids, why)
    return {
        "modality": Modality.ACTION.value,
        "performed": 0,
        "refusal": why,
        "spoken_response": Spoken(spoken).to_text(),
        "console_url": _console("/dashboard"),
        **extra,
    }


def request_risk_acceptance(alert_id: str, reason: str = "") -> dict:
    """Allow ONE finding in a single step: the person accepts the risk and the finding
    stops blocking. Only completes if BOTH are true: blocked_pull_requests or
    finding_detail was called for this exact alert_id within the last ten minutes (the
    person has seen it on a screen, not just heard a count), and a reason was given.
    Missing either: refused, nothing is changed, and the reply says where to decide
    instead. If they haven't seen it yet, offer to show it before asking for a decision.
    If they gave no reason, ask for it before calling this again. To block a finding, or
    to decide several, use stage_decisions."""
    # Why "reviewed, then voice-confirmed" is allowed while "ignore it, go ahead" with no
    # review is not: this module's docstring. Why the check is a time window on the alert
    # rather than a session: src/storage/session_store.py.
    reason = (reason or "").strip()
    alert = get_alert(alert_id)
    target = _console(f"/dashboard#{alert_id}") if alert else _console("/dashboard")

    if alert is not None and alert.status == "resolved":
        return {**_refusal("request_risk_acceptance", "already_resolved", "That one was already decided.",
                           alert_ids=[alert_id]), "console_url": target}

    reviewed = alert is not None and session_store.was_shown_recently(alert_id)

    if reviewed and reason:
        if len(reason) > MAX_REASON_CHARS:
            return {**_refusal("request_risk_acceptance", "reason_too_long",
                               "That reason is too long. Please give it in a sentence or two.",
                               alert.repo, alert.pr_number, [alert_id]), "console_url": target}
        # Built before anything is written. The reason is not echoed back: it is on the
        # record already, and a long one must never turn a decision that was made into an error.
        spoken = Spoken("Done. Allowed, on the record.").to_text()
        try:
            _resolve_one(alert, DECISION_ALLOW, reason)
        except AlertNotFoundOrAlreadyResolved:
            return {**_refusal("request_risk_acceptance", "already_resolved", "That one was already decided.",
                               alert.repo, alert.pr_number, [alert_id]), "console_url": target}
        _write_back([alert])
        audit("request_risk_acceptance", "applied", alert.repo, alert.pr_number, [alert_id], applied=1)
        return {
            "modality": Modality.ACTION.value,
            "performed": 1,
            "alert_id": alert_id,
            "spoken_response": spoken,
            "console_url": target,
        }

    # Report the review gate first — it's the one the user's own worked
    # example (ask, hear a count, say "ignore it, go ahead" with nothing
    # ever shown) has to fail on, regardless of whether a reason was also
    # given in the same breath.
    if not reviewed:
        missing, spoken = "for this to have been shown on a screen recently", "Take a look on a screen first, then tell me why."
        why = "not_seen"
    else:
        missing, spoken = "a reason", "That needs a written reason on the record."
        why = "no_reason"
    audit("request_risk_acceptance", "refused", alert.repo if alert else None,
          alert.pr_number if alert else None, [alert_id], why)

    return {
        "modality": Modality.ACTION.value,
        "performed": 0,
        "reason": "requires_attested_human_decision",
        "explanation": (
            f"Allowing a finding accepts a compliance risk, which needs {missing}, and a durable record either way. "
            "That's the same human accountability SHIP enforces on the code it reviews."
        ),
        "spoken_response": Spoken(spoken, screen_hint="Opening it on your screen.").to_text(),
        "console_url": target,
    }


def _findings_for(repo: str, pr_number: int, status=None):
    """The ordered findings for one PR, exactly as currently on screen —
    shared by stage_decisions and proceed so "position 2" means the same
    finding in both. None if that PR has no active findings right now.
    `status` lets a caller that has just read it avoid a second scan."""
    status = status if status is not None else _findings_detail()
    for pr in status.pull_requests:
        if pr.repo == repo and pr.pr_number == pr_number:
            return pr.findings
    return None


def stage_decisions(repo: str, pr_number: int, decisions: list[dict], device: str = "") -> dict:
    """Captures a decision for one or more findings of one pull request WITHOUT
    resolving anything yet. Each item in `decisions` is {"alert_id": str, "decision":
    "allow" or "block", "reason": str, "position": int (optional, only to word a
    refusal)}. alert_id comes from what the screen showed. "allow" accepts the risk,
    so the finding stops blocking; "block" confirms the finding is real, so the pull
    request stays blocked until the code is fixed. decision must be exactly "allow"
    or "block": anything else, and the old `approved` flag, is refused. Nothing
    changes until proceed() is called: say what was captured and ask the person to
    confirm. If `device` is given, the pending state is pushed there. Every finding
    must be open, shown on a screen recently, and have a reason, or the WHOLE batch is
    refused with a plain sentence."""
    # alert_id rather than a number: a number on screen can drift when findings arrive, an
    # id cannot. See docs/voice-resolution-workflow.md.
    if not isinstance(decisions, list) or not decisions:
        return _refusal("stage_decisions", "no_decisions", "Which findings do you want to decide, and why?",
                        repo, pr_number)

    status = _findings_detail()
    findings = _findings_for(repo, pr_number, status)
    if not findings:
        return _refusal("stage_decisions", "no_open_findings",
                        f"I don't see any open findings on {repo} #{pr_number} right now.", repo, pr_number)

    position_of = {f.alert_id: i for i, f in enumerate(findings, start=1)}
    ids = [d.get("alert_id") for d in decisions if isinstance(d, dict)]
    staged, seen_ids = [], set()
    for d in decisions:
        if not isinstance(d, dict) or not isinstance(d.get("alert_id"), str) or not d["alert_id"]:
            return _refusal("stage_decisions", "missing_alert_id",
                            "I couldn't tell which finding you meant. Which one?", repo, pr_number, ids)
        alert_id = d["alert_id"]
        hint = d.get("position")
        what = f"finding {hint}" if isinstance(hint, int) and not isinstance(hint, bool) and 0 < hint < 1000 else "one of those"
        if alert_id in seen_ids:
            return _refusal("stage_decisions", "duplicate", "You mentioned the same finding twice. Which decision did you mean?",
                            repo, pr_number, ids)
        seen_ids.add(alert_id)

        if alert_id not in position_of:
            try:
                existing = get_alert(alert_id)
            except Exception:  # noqa: BLE001
                log.exception("could not look up %s while staging", alert_id)
                return _refusal("stage_decisions", "lookup_failed",
                                "I couldn't check that just now. Please try again.", repo, pr_number, ids)
            if existing is not None and existing.status == "resolved":
                return _refusal("stage_decisions", "already_resolved",
                                f"{what.capitalize()} was already decided in the console.", repo, pr_number, ids)
            return _refusal("stage_decisions", "not_found",
                            f"{what.capitalize()} is no longer there. Take a fresh look at the screen.", repo, pr_number, ids)

        if "approved" in d:
            return _refusal("stage_decisions", "approved_argument_retired",
                            'Decisions no longer take "approved". Send decision as "allow" or "block".',
                            repo, pr_number, ids)
        if not is_decision(d.get("decision")):
            return _refusal("stage_decisions", "decision_unclear",
                            "I couldn't tell whether to allow or block that. Which was it?", repo, pr_number, ids)
        if not session_store.was_shown_recently(alert_id):
            return _refusal("stage_decisions", "not_seen",
                            f"{what.capitalize()} hasn't been shown on a screen recently. Take a look first.",
                            repo, pr_number, ids)
        reason = str(d.get("reason") or "").strip()
        if not reason:
            return _refusal("stage_decisions", "no_reason", f"What's the reason for {what}?", repo, pr_number, ids)
        if len(reason) > MAX_REASON_CHARS:
            return _refusal("stage_decisions", "reason_too_long",
                            "That reason is too long. Please give it in a sentence or two.", repo, pr_number, ids)
        staged.append({"position": position_of[alert_id], "alert_id": alert_id,
                       "decision": d["decision"], "reason": reason})

    spoken = Spoken("Got it, take a look and say proceed when you're ready.").to_text()
    pending_store.stage(repo, pr_number, staged)
    audit("stage_decisions", "staged", repo, pr_number, [s["alert_id"] for s in staged], staged=len(staged))

    if device:
        push_to_device(device.strip().lower(), _findings_payload(status))

    return {
        "modality": Modality.ACTION.value,
        "performed": 0,
        "staged": len(staged),
        "spoken_response": spoken,
        "console_url": _console("/dashboard"),
    }


def _apply_staged(d: dict) -> tuple[str, object]:
    """Commits one staged decision, re-checking at commit time everything that was checked
    at staging time. Returns (outcome, alert-or-None). Never raises, so one item cannot
    abort the batch."""
    # A row staged by code that wrote decisions another way has no readable decision. It is
    # never guessed at: nothing is applied and the person is asked to say it again.
    if (not isinstance(d, dict) or not isinstance(d.get("alert_id"), str) or not is_decision(d.get("decision"))
            or not str(d.get("reason") or "").strip()):
        return "unreadable", None
    alert_id = d["alert_id"]
    try:
        alert = get_alert(alert_id)
    except Exception:  # noqa: BLE001
        log.exception("could not read %s while committing", alert_id)
        return "error", None
    if alert is None:
        return "not_found", None
    if alert.status == "resolved":
        return "already_resolved", None
    if not session_store.was_shown_recently(alert_id):
        return "skipped_not_seen", None
    try:
        _resolve_one(alert, d["decision"], d["reason"])
    except AlertNotFoundOrAlreadyResolved:
        return "already_resolved", None
    except Exception:  # noqa: BLE001
        log.exception("could not record the decision for %s", alert_id)
        return "error", None
    return "applied", alert


def _plural_were(n: int) -> str:
    return "wasn't" if n == 1 else "weren't"


def _proceed_sentence(outcomes: list[dict]) -> str:
    """What happened, in one capped sentence, from the per-finding outcomes."""
    n = len(outcomes)
    count = {k: sum(1 for o in outcomes if o["outcome"] == k)
             for k in ("applied", "skipped_not_seen", "already_resolved", "not_found", "unreadable", "error")}
    if count["applied"] == n:
        return "Done." if n == 1 else f"Done. Applied all {n}."
    if count["error"] == n:
        return "I couldn't save that just now. Say proceed to try again."
    if count["unreadable"] == n:
        return "I couldn't read what was staged, so I applied nothing. Please say your decisions again."
    parts = [f"Applied {count['applied']} of {n}."]
    if count["skipped_not_seen"]:
        k = count["skipped_not_seen"]
        parts.append(f"{k} {_plural_were(k)} shown on a screen recently.")
    if count["already_resolved"]:
        parts.append(f"{count['already_resolved']} already decided in the console.")
    if count["not_found"]:
        k = count["not_found"]
        parts.append(f"{k} no longer {'exists' if k == 1 else 'exist'}.")
    if count["unreadable"]:
        parts.append(f"{count['unreadable']} couldn't be read, say {'it' if count['unreadable'] == 1 else 'them'} again.")
    if count["error"]:
        parts.append(f"{count['error']} could not be saved, say proceed to retry.")
    sentence = " ".join(parts)
    if len(sentence) > SPEECH_CHAR_BUDGET:
        sentence = f"Applied {count['applied']} of {n}. Some were left alone, check the screen."
    return sentence


def proceed(repo: str, pr_number: int, device: str = "") -> dict:
    """Commits whatever stage_decisions most recently staged for this PR — the one
    action that writes the batch. Call it ONLY after the person has confirmed what was
    staged ("proceed", "yes, go ahead"). It re-checks every finding at commit time and
    reports what actually happened per finding in `outcomes` (applied, skipped_not_seen,
    already_resolved, not_found, unreadable, error); `performed` is how many were applied, and the
    spoken sentence says so, including what was left alone. A finding that could not be
    saved because of a temporary problem stays staged for another proceed. "Nothing
    pending" if nothing was staged or it has been more than ten minutes (see
    src/storage/pending_store.py): ask them to run through their decisions again."""
    got = pending_store.get_pending_with_age(repo, pr_number)
    if not got:
        return _refusal("proceed", "nothing_pending", "Nothing pending to confirm.", repo, pr_number,
                        outcomes=[], of=0, pending_remaining=0)
    staged, staged_at = got

    outcomes, applied = [], []
    for d in staged:
        outcome, alert = _apply_staged(d)
        outcomes.append({"alert_id": d.get("alert_id") if isinstance(d, dict) else None, "outcome": outcome})
        if alert is not None:
            applied.append(alert)

    # Rule for what stays staged: a decision that reached a final answer (applied, or
    # skipped/gone/already decided, all of which need the person to look again) is
    # finished with. One that failed for a transient reason is kept, with its original
    # clock, so saying proceed again retries it without extending how long the
    # confirmation counts.
    failed = [d for d, o in zip(staged, outcomes) if o["outcome"] == "error"]
    if failed:
        try:
            pending_store.stage(repo, pr_number, failed, staged_at=staged_at)
        except Exception:  # noqa: BLE001
            log.exception("could not keep the failed decisions staged for %s#%s", repo, pr_number)
    else:
        pending_store.clear_pending(repo, pr_number)

    gate = _write_back(applied) if applied else None
    spoken = _proceed_sentence(outcomes)

    if device:
        push_to_device(device.strip().lower(), _findings_payload(_findings_detail(gate)))

    audit("proceed", "applied" if len(applied) == len(outcomes) else "partial", repo, pr_number,
          [o["alert_id"] for o in outcomes if o["alert_id"]], applied=len(applied), of=len(outcomes),
          errors=len(failed))
    return {
        "modality": Modality.ACTION.value,
        "performed": len(applied),
        "of": len(outcomes),
        "outcomes": outcomes,
        "pending_remaining": len(failed),
        "spoken_response": Spoken(spoken).to_text(),
        "console_url": _console("/dashboard"),
    }


def cancel_pending(repo: str, pr_number: int, device: str = "") -> dict:
    """Drops whatever stage_decisions staged for this PR, without resolving anything.
    Used when the person says no to a staged batch, so the screen stops showing it as
    pending. Nothing in ship-alerts changes."""
    pending_store.clear_pending(repo, pr_number)
    if device:
        push_to_device(device.strip().lower(), _findings_payload(_findings_detail()))
    audit("cancel_pending", "cancelled", repo, pr_number)
    return {
        "modality": Modality.ACTION.value,
        "performed": 0,
        "spoken_response": Spoken("Okay, I won't apply it.").to_text(),
        "console_url": _console("/dashboard"),
    }


def _notify_registered_device(name: str, status, payload: dict) -> bool:
    """No page is open on this screen, but if the person registered it for alerts, hold the
    findings for it and send a notification. The findings are NOT marked as shown here: a
    notification is not a display. The page reports that it displayed them once the person
    opens it (the ack route in device_gateway_handler.py)."""
    try:
        if not push_store.subscriptions_for_device(name):
            return False
        alert_ids = [f.alert_id for pr in status.pull_requests for f in pr.findings]
        push_store.save_pending(name, payload, alert_ids)
        count = len(alert_ids)
        sent = webpush.notify_device(
            name, f"{count} finding{'s' if count != 1 else ''} need{'' if count != 1 else 's'} a decision. Tap to review.")
        return sent > 0
    except Exception:  # noqa: BLE001 - an unreachable screen is a normal answer, not an error
        log.exception("could not notify %s", name)
        return False


def display_on(surface: str) -> dict:
    """Route the current findings to a display surface the person chose —
    for example "tv", "ipad", "laptop", or "here". Use when someone says
    "show me on the TV" or after release_status offers a screen.

    "here" means the device asking: no push happens, the caller renders its
    own copy of the findings. Any other name is a real push over that
    device's open WebSocket connection (see src/relay/push.py) to whatever
    display page most recently registered under that name, reported
    honestly if the named device isn't connected.

    The findings count as shown, and so open to a spoken decision, only
    when a screen actually received them. A push to a screen that is not
    connected shows nothing, so it records nothing. On success the reply
    carries `routing`: the position and id of each open finding, which is
    how a later spoken decision points at one."""
    # A name is spoken back, so keep it short and plain whatever was sent.
    normalised = re.sub(r"[^a-z0-9 _-]", "", (surface or "").strip().lower())[:30].strip() or "here"
    status = _findings_detail()

    if normalised == "here":
        _mark_shown(status)   # the caller renders its own copy of the same payload
        audit("display_on", "shown", surface="here", findings=sum(len(p.findings) for p in status.pull_requests))
        return {
            "modality": Modality.ACTION.value, "surface": normalised, "delivered": True,
            "routing": _routing(status),
            "console_url": _console("/dashboard"),
            "spoken_response": Spoken("Showing it here.").to_text(),
        }

    payload = _findings_payload(status)
    result = push_to_device(normalised, payload)
    delivered = bool(result["delivered_to"])      # only connections whose post succeeded are listed
    notified = False
    if delivered:
        _mark_shown(status)
    else:
        notified = _notify_registered_device(normalised, status, payload)

    if delivered:
        spoken = f"Putting it on your {normalised}."
    elif notified:
        spoken = f"I've sent a notification to your {normalised}. Open it to review."
    else:
        spoken = f"I don't see a {normalised} connected right now."
    audit("display_on", "delivered" if delivered else ("notified" if notified else "not_connected"),
          surface=normalised, connections=len(result["delivered_to"]))

    return {
        "modality": Modality.ACTION.value,
        "surface": normalised,
        "delivered": delivered,
        "notified": notified,
        "routing": _routing(status) if delivered else [],
        "console_url": _console("/dashboard"),
        "spoken_response": Spoken(spoken).to_text(),
    }


def findings_routing() -> dict:
    """Where each open finding sits and whether the person has seen it on a screen in the
    last ten minutes. Positions, ids and a yes or no only: nothing that must stay off the
    speaker. `position` is where the finding sits now; `shown_position` is the number it
    had on the screen that displayed it, which is the number the person will speak (null
    if it was shown another way). Reading this records nothing as shown. Use it to know
    which findings a spoken decision may point at."""
    status = _findings_detail()
    pull_requests = []
    for pr in status.pull_requests:
        findings = []
        for i, f in enumerate(pr.findings, start=1):
            view = session_store.recent_view(f.alert_id)
            findings.append({"position": i, "alert_id": f.alert_id, "blocks_merge": f.severity == SEVERITY_BLOCKING,
                             "seen": view is not None, "shown_position": view["position"] if view else None})
        pull_requests.append({"repo": pr.repo, "pr_number": pr.pr_number, "findings": findings})
    return {"modality": Modality.ACTION.value, "pull_requests": pull_requests}


def connected_screens() -> dict:
    """Which screens are set up right now, by the name each one gave itself
    ("tv", "laptop", "tablet"). SAFE TO SPEAK: names only. Use before offering
    a choice of screens, so the person is never offered one that is not there.
    A screen counts if its page is open now or if it was registered for alerts
    (it is then reached by a notification). An empty list means neither exists:
    say so, and offer another way to review (the review console)."""
    live = {c.device_name for c in list_all_connections()}
    try:
        registered = push_store.subscribed_device_names()
    except Exception:  # noqa: BLE001 - the live screens still count if the registry cannot be read
        log.exception("could not read the registered screens")
        registered = set()
    return {
        "modality": Modality.ACTION.value,
        "screens": sorted(live | registered),
        "open_now": sorted(live),
        "notify_only": sorted(registered - live),
        "console_url": _console("/dashboard"),
    }


TOOLS = (release_status, blocked_pull_requests, finding_detail, request_risk_acceptance,
         stage_decisions, proceed, cancel_pending, display_on, connected_screens, findings_routing)


def create_app() -> Starlette:
    """Builds a fresh FastMCP instance and its ASGI app. See this module's
    ARCHITECTURE NOTE above for why this must be a factory rather than a
    module-level singleton on Lambda. Local dev (uvicorn) also uses this,
    via the module-level `mcp`/`app` below, where the single-instance
    lifetime of a long-running process makes the singleton constraint a
    non-issue — the factory is what makes both cases correct with one
    implementation instead of two."""
    fresh = FastMCP(
        "ship-relay",
        instructions=INSTRUCTIONS,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ALLOWED_HOSTS,
            allowed_origins=ALLOWED_ORIGINS,
        ),
        # Relay holds no state of its own — every tool re-reads storage
        # fresh on every call — so there is nothing a cross-request
        # session would even be caching. Independently required on Lambda
        # regardless (see ARCHITECTURE NOTE), but correct on its own terms.
        stateless_http=True,
        # REQUIRED on a Lambda Function URL, confirmed by a real deployed
        # failure: the MCP spec allows a Streamable HTTP server to answer
        # either as a single buffered JSON response or as an SSE stream,
        # and the SDK defaults to SSE (is_json_response_enabled=False,
        # see mcp.server.streamable_http.StreamableHTTPServerTransport).
        # An SSE response carries `Connection: keep-alive`, which a Lambda
        # Function URL's default BUFFERED invoke mode cannot reconcile —
        # reproduced against the real deployed endpoint: a direct
        # aws lambda invoke succeeded in 375ms with a correct response
        # body, while the SAME request through the Function URL failed in
        # ~240ms with a generic timeout error, isolating the fault to that
        # HTTP layer specifically, not the function's own logic. Relay's
        # tools are simple synchronous request/response calls with nothing
        # to stream incrementally, so plain JSON is not a compromise here
        # — it is the correct shape for what Relay actually does, and it
        # is a first-class, spec-legitimate SDK option, not a workaround.
        json_response=True,
    )
    for fn in TOOLS:
        fresh.tool()(fn)
    raw_app = fresh.streamable_http_app()

    # transport_security (above) is a DENY GATE — it rejects a disallowed
    # Host/Origin with 403/421, but confirmed by a real local test NOT to
    # add the Access-Control-Allow-Origin response header even for an
    # ALLOWED origin. A browser enforces CORS independently of whatever
    # the server permits: without this header, a genuine cross-origin
    # fetch() from the Alexa+ simulator page would succeed at the network
    # level and then be silently blocked from JavaScript reading the
    # response, in the browser console only — invisible to curl, and
    # exactly the kind of gap a local request-level test alone would
    # have missed here. CORSMiddleware adds the actual response headers;
    # transport_security still does the deeper Host-based rebinding
    # check underneath it.
    return CORSMiddleware(
        raw_app,
        allow_origins=ALLOWED_ORIGINS,
        allow_methods=["POST", "GET", "OPTIONS"],
        allow_headers=["content-type", "mcp-session-id", "mcp-protocol-version", "accept"],
    )


# Module-level instance for local development (`uvicorn src.relay.server:app`)
# and for tests that only need one request/response cycle. Lambda NEVER
# imports this — see relay_lambda_handler.py, which calls create_app() fresh
# on every invocation instead.
mcp = FastMCP(
    "ship-relay", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=ALLOWED_HOSTS, allowed_origins=ALLOWED_ORIGINS,
    ),
)
for _fn in TOOLS:
    mcp.tool()(_fn)
app = CORSMiddleware(
    mcp.streamable_http_app(),
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["content-type", "mcp-session-id", "mcp-protocol-version", "accept"],
)
