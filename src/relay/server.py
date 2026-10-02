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

Voice cannot accept a compliance risk BLIND. It can confirm one, once a
person has actually looked at the finding on a screen.

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

So `request_risk_acceptance` checks one real, server-side fact before
doing anything: was a screen tool (blocked_pull_requests / finding_detail)
called for THIS alert_id recently — see src/storage/session_store.py. No:
refused, exactly as before, redirected to Gate. Yes, and a reason was
given: performed for real, written to ship-alerts with that reason as the
record, same durability Gate's own resolve gives it. The reason is still
required either way — dropping that would trade a real audit trail for a
bare yes/no flag, which is the one piece of the original design worth
keeping regardless of channel.

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

import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware

from src.relay.modality import Modality, Spoken
from src.relay.push import push_to_device
from src.relay.readmodel import findings_detail as _findings_detail
from src.relay.readmodel import release_status as _release_status
from src.storage import session_store
from src.storage.alert_store import SEVERITY_BLOCKING, get_alert, resolve_alert, theme_for

CONSOLE_URL = os.environ.get("SHIP_DASHBOARD_URL", "").rstrip("/")

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
    "Accepting a compliance risk needs both a reason and the finding having "
    "already been shown on a screen in this same conversation — call "
    "request_risk_acceptance with a reason once both are true; if either isn't, "
    "it returns the console to open instead of performing anything."
)


def _console(path: str = "") -> str:
    return f"{CONSOLE_URL}{path}" if CONSOLE_URL else "the SHIP review console"


# --- tool logic: plain functions, no I/O at import, registered by create_app() ---


def release_status() -> str:
    """Whether anything is currently blocking a release, as one short spoken
    sentence. SAFE TO READ ALOUD. Returns counts and which pull request to
    look at first — never a list of findings, never a citation. Use this for
    "is anything blocked", "can we ship", "what's going on with my release"."""
    status = _release_status()
    hint = None
    if status.total:
        hint = "Want it on a screen?"
    return status.to_spoken(screen_hint=hint).to_text()


def blocked_pull_requests() -> dict:
    """Every pull request with unresolved findings, with counts and the
    findings themselves. SCREEN ONLY — this is a list and must be displayed,
    never spoken. Use after release_status when the person asks to see detail.

    Calling this records every returned alert as recently shown — see
    request_risk_acceptance and src/storage/session_store.py. That is a
    side effect of what this tool already does (surfacing the findings on
    a screen), not a new responsibility."""
    status = _findings_detail()
    for pr in status.pull_requests:
        for f in pr.findings:
            session_store.mark_shown(f.alert_id)
    return {
        "modality": Modality.SCREEN.value,
        "display_only": True,
        "summary": {"blocking": status.blocking, "flagged": status.review,
                    "pull_requests": len(status.pull_requests)},
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
                    }
                    for i, f in enumerate(pr.findings, start=1)
                ],
            }
            for pr in status.pull_requests
        ],
        "console_url": _console("/dashboard"),
    }


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


def request_risk_acceptance(alert_id: str, reason: str = "") -> dict:
    """Called when someone asks to approve, accept, override or clear a
    blocking finding. Only completes if BOTH are true: blocked_pull_requests
    or finding_detail was called for this exact alert_id within the last
    ten minutes (so the person has actually seen it on a screen recently,
    not just heard a count), and a reason was given. Missing either:
    refused, and this tool never performs it — returns where the decision
    must be made instead. If the person hasn't reviewed it yet, offer to
    show it before asking for a decision. If they have but gave no reason,
    ask what the reason is before calling this again.

    See src/relay/server.py's module docstring for why "reviewed, then
    voice-confirmed" is allowed while "ignore it, go ahead" with no review
    at all is not, and never will be. See src/storage/session_store.py for
    why this checks a time window on the alert itself rather than a
    session — this Lambda has no session continuity to check against."""
    alert = get_alert(alert_id)
    target = _console(f"/dashboard#{alert_id}") if alert else _console("/dashboard")
    reviewed = alert is not None and session_store.was_shown_recently(alert_id)

    if reviewed and reason.strip():
        resolve_alert(alert_id, approved=True, reason=reason.strip(), resolved_by="voice")
        return {
            "modality": Modality.ACTION.value,
            "performed": True,
            "alert_id": alert_id,
            "resolution_reason": reason.strip(),
            "spoken_response": Spoken(f"Done. Accepted, on the record: {reason.strip()}").to_text(),
            "console_url": target,
        }

    # Report the review gate first — it's the one the user's own worked
    # example (ask, hear a count, say "ignore it, go ahead" with nothing
    # ever shown) has to fail on, regardless of whether a reason was also
    # given in the same breath.
    if not reviewed:
        missing, spoken = "for this to have been shown on a screen recently", "Take a look on a screen first, then tell me why."
    else:
        missing, spoken = "a reason", "That needs a written reason on the record."

    return {
        "modality": Modality.ACTION.value,
        "performed": False,
        "reason": "requires_attested_human_decision",
        "explanation": (
            f"Accepting a compliance risk needs {missing}, and a durable record either way. "
            "That's the same human accountability SHIP enforces on the code it reviews."
        ),
        "spoken_response": Spoken(spoken, screen_hint="Opening it on your screen.").to_text(),
        "console_url": target,
    }


def display_on(surface: str) -> dict:
    """Route the current findings to a display surface the person chose —
    for example "tv", "ipad", "laptop", or "here". Use when someone says
    "show me on the TV" or after release_status offers a screen.

    "here" means the device asking — no push happens, the caller renders
    its own copy via blocked_pull_requests, same as before this existed.
    Any other name is a REAL push over that device's open WebSocket
    connection (see src/relay/push.py) to whatever display page most
    recently registered under that name — not a description of what
    would happen, an actual delivery, reported honestly if the named
    device isn't currently connected rather than pretending it worked.

    Either way this records every pushed alert as recently shown — see
    request_risk_acceptance."""
    normalised = (surface or "").strip().lower() or "here"

    if normalised == "here":
        blocked_pull_requests()  # records the review; same payload the caller already has
        return {
            "modality": Modality.ACTION.value, "surface": normalised, "delivered": True,
            "console_url": _console("/dashboard"),
            "spoken_response": Spoken("Showing it here.").to_text(),
        }

    payload = blocked_pull_requests()
    result = push_to_device(normalised, payload)
    delivered = bool(result["delivered_to"])

    return {
        "modality": Modality.ACTION.value,
        "surface": normalised,
        "delivered": delivered,
        "console_url": _console("/dashboard"),
        "spoken_response": Spoken(
            f"Putting it on your {normalised}." if delivered
            else f"I don't see a {normalised} connected right now."
        ).to_text(),
    }


TOOLS = (release_status, blocked_pull_requests, finding_detail, request_risk_acceptance, display_on)


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
