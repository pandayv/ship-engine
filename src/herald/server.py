"""
Herald — SHIP's MCP server, the layer that lets an assistant ask SHIP
questions without a human opening a browser.

Herald carries no judgment. Detector already reasoned over the code,
Triage already decided whether it blocks, Gate already recorded whatever
a human concluded. Herald only reads that and routes it to a surface.
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
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from src.herald.modality import Modality, Spoken
from src.herald.readmodel import findings_detail as _findings_detail
from src.herald.readmodel import release_status as _release_status
from src.storage.alert_store import SEVERITY_BLOCKING, get_alert

CONSOLE_URL = os.environ.get("SHIP_DASHBOARD_URL", "").rstrip("/")

mcp = FastMCP(
    "ship-herald",
    instructions=(
        "SHIP reviews pull requests for AI-compliance risk and can block a merge. "
        "Use release_status for any spoken answer — it is one sentence and is the only "
        "tool safe to read aloud. Never read findings, citations or code aloud: call "
        "blocked_pull_requests or finding_detail and render the result on a screen. "
        "Accepting a compliance risk cannot be done by voice; call "
        "request_risk_acceptance, which returns the console to open."
    ),
)


def _console(path: str = "") -> str:
    return f"{CONSOLE_URL}{path}" if CONSOLE_URL else "the SHIP review console"


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def display_on(surface: str) -> dict:
    """Route the current findings to a display surface the person chose —
    for example "tv", "laptop", "phone", or "here". Use when someone says
    "show me on the TV" or after release_status offers a screen."""
    normalised = (surface or "").strip().lower() or "here"
    status = _release_status()
    return {
        "modality": Modality.ACTION.value,
        "surface": normalised,
        "payload": "blocked_pull_requests",
        "console_url": _console("/dashboard"),
        "spoken_response": Spoken(
            f"Putting it on your {normalised}." if normalised != "here"
            else "Showing it here."
        ).to_text(),
        "summary": {"blocking": status.blocking, "flagged": status.review},
    }


app = mcp.streamable_http_app()
