"""
Relay — SHIP's MCP server, the layer that lets an assistant ask SHIP
questions without a human opening a browser.

Relay carries no judgment. Detector already reasoned over the code,
Triage already decided whether it blocks, Gate already recorded whatever
a human concluded. Relay only reads that and routes it to a surface.
That is a design principle, and Alexa+'s 500 ms round-trip budget also
makes it the only possible shape — a diagnosis takes ~3 s, ten times the
whole budget.

WHAT IS DELIBERATELY MISSING, AND WHY IT IS THE POINT

There is no tool here that accepts a compliance risk by voice.

That is not a backlog item. SHIP exists to stop AI systems from making
consequential decisions with no human accountably in the loop — that is
literally what detector TLGP-002 flags in other people's code. A version
of SHIP that let someone clear a blocking GDPR finding by saying "approve
it" to a speaker would be committing the exact violation it was built to
catch, in its own interface.

So accepting risk requires what the regulation requires of everyone else:
a named person, a written reason, and a record. `request_risk_acceptance`
below exists precisely so that asking produces a clear, honest redirect
to a surface where those things can happen, rather than a vague failure.
The refusal is enforced here, in the server, where no prompt and no
client can argue with it.

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
from src.storage.alert_store import SEVERITY_BLOCKING, get_alert

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
    "Accepting a compliance risk cannot be done by voice; call "
    "request_risk_acceptance, which returns the console to open."
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
    never spoken. Use after release_status when the person asks to see detail."""
    status = _findings_detail()
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
                        "alert_id": f.alert_id,
                        "file": f.file,
                        "severity": f.severity,
                        "blocks_merge": f.severity == SEVERITY_BLOCKING,
                        "risk_score": f.risk_score,
                        "what_is_wrong": f.plain_english_summary,
                        "citation": f.citation,
                    }
                    for f in pr.findings
                ],
            }
            for pr in status.pull_requests
        ],
        "console_url": _console("/dashboard"),
    }


def finding_detail(alert_id: str) -> dict:
    """Full detail for one finding: what is wrong, the regulation it breaks,
    and the suggested remediation. SCREEN ONLY — contains a citation and may
    contain code, neither of which should ever be read aloud."""
    alert = get_alert(alert_id)
    if alert is None:
        return {"modality": Modality.SCREEN.value, "found": False, "alert_id": alert_id}
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


def request_risk_acceptance(alert_id: str) -> dict:
    """Called when someone asks to approve, accept, override or clear a
    blocking finding. This CANNOT be completed by voice and this tool never
    performs it. It returns where the decision must be made instead.

    Accepting a compliance risk requires a named person, a written reason and
    a durable record — the same human accountability SHIP enforces on the code
    it reviews. Speak the `spoken_response` verbatim and open `console_url`."""
    alert = get_alert(alert_id)
    target = _console(f"/dashboard#{alert_id}") if alert else _console("/dashboard")
    return {
        "modality": Modality.ACTION.value,
        "performed": False,
        "reason": "requires_attested_human_decision",
        "explanation": (
            "Accepting a compliance risk needs a named person, a written reason, and a "
            "record. That cannot happen over voice, so SHIP does not offer it here."
        ),
        "spoken_response": Spoken(
            "That needs a written reason on the record.",
            screen_hint="Opening it on your screen.",
        ).to_text(),
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
    device isn't currently connected rather than pretending it worked."""
    normalised = (surface or "").strip().lower() or "here"

    if normalised == "here":
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
