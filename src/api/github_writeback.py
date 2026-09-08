"""
Writes SHIP's verdicts and the human's decisions back to the pull request
itself, so a finding exists where the work happens rather than only in a
dashboard someone has to remember to open.

Three things get written:
  - a commit status ("ship/compliance") that turns the merge button red
    when something is genuinely blocking
  - a comment per finding, carrying the plain-English explanation, the
    citation, and remediation in whatever shape that finding's category
    actually admits (a patch, a requirement, or a product decision)
  - a comment per human decision, recording who decided what and why

Why commit statuses and not the Checks API: Checks requires a GitHub App
installation. Statuses work with the plain token this service already
holds, and a status can be made a required check in branch protection,
which is the part that actually blocks a merge. The richer inline-
annotation UX Checks would buy is not worth a second auth mechanism here.

RACE SAFETY — the important design decision in this file. Fragments are
processed in parallel by independently-scaling Lambdas (see main.py's
dispatch docstring), so no single fragment knows the PR's overall state.
If each one posted its own view, a fragment that happened to finish last
with a clean verdict could overwrite a blocking one with "passing".

So no caller ever states the status directly. sync_pr_check() recomputes
it from DynamoDB — the shared source of truth — and posts what it derives.
Every writer computes from the same data, so concurrent writes converge on
the same answer instead of fighting, ordering stops mattering, and an
intermediate state is still truthful ("2 findings blocking, so far"). The
same function serves the human-decision path, so resolving the last
blocking alert flips the check green through exactly the code that turned
it red.

FAILURE POLICY: every function here fails soft, and logs loudly. GitHub
being briefly unreachable must not crash a fragment — the alert is already
stored idempotently, and raising would send the message back through SQS
for a retry that re-runs the model call and spends real Bedrock quota to
redeliver a comment. But a compliance gate that silently fails to block is
the worse error of the two, so every failure is logged at ERROR with the
repo, PR, and reason, rather than swallowed quietly.
"""

import logging
import os

import requests

from src.storage.alert_store import SEVERITY_BLOCKING, list_alerts_for_pr

log = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
STATUS_CONTEXT = "ship/compliance"
REQUEST_TIMEOUT = 10

# Set to the Attending dashboard's public URL so a status check links
# straight to the review console. Unset is fine — the status still posts.
DASHBOARD_URL = os.environ.get("SHIP_DASHBOARD_URL", "")


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "")


def _writeback_enabled(repo_full_name: str) -> bool:
    """Skips the local-testing pseudo-repo and any environment with no
    token configured, so the test suite and local runs need no network."""
    if repo_full_name == "local-test":
        return False
    if not _token():
        log.info("GitHub write-back skipped: no GITHUB_TOKEN configured")
        return False
    return True


def _post(url: str, body: dict, what: str) -> bool:
    try:
        response = requests.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        # Deliberately not re-raised — see this module's FAILURE POLICY.
        log.error("GitHub write-back failed (%s): %s — url=%s", what, e, url)
        return False


def _severity_line(alert) -> str:
    return "merge blocked" if alert.severity == SEVERITY_BLOCKING else "flagged, merge not stopped"


def comment_for_finding(alert) -> str:
    blocking = alert.severity == SEVERITY_BLOCKING
    parts = [
        f"### Compliance finding — {'merge blocked' if blocking else 'flagged for review'}",
        "",
        f"`{alert.file}` · risk {alert.risk_score}"
        + ("" if blocking else " · this does not stop the merge"),
        "",
        alert.plain_english_summary,
        "",
        f"**{alert.citation}**",
    ]
    if alert.remediation_patch:
        parts += ["", "<details><summary>Suggested change</summary>", "",
                  "```python", alert.remediation_patch, "```", "", "</details>"]
    parts += ["", "---", ""]
    if DASHBOARD_URL:
        parts.append(f"Found by SHIP. A person can accept this risk or confirm it in the "
                     f"[review console]({DASHBOARD_URL}).")
    else:
        parts.append("Found by SHIP. A person reviews this before it can be resolved.")
    return "\n".join(parts)


def comment_for_decision(alert, accepted: bool, reason: str, who: str) -> str:
    heading = ("### Risk accepted — merge unblocked" if accepted
               else "### Confirmed — this needs a fix")
    outcome = ("SHIP's check on this commit has been set to passing."
               if accepted else
               "SHIP's check stays failing until a new commit resolves it.")
    return "\n".join([
        heading,
        "",
        f"**{who}** decided this on the finding in `{alert.file}` ({alert.citation}, risk {alert.risk_score}).",
        "",
        f"> {reason}",
        "",
        f"This decision is recorded and attributed. {outcome}",
    ])


def post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> bool:
    if not _writeback_enabled(repo_full_name):
        return False
    return _post(
        f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments",
        {"body": body},
        what="pr comment",
    )


def sync_pr_check(repo_full_name: str, pr_number: int, head_sha: str) -> bool:
    """
    Recomputes the commit status from stored alerts and posts it. Safe to
    call from any number of concurrent fragments and from the dashboard —
    see this module's RACE SAFETY note for why that holds.
    """
    if not _writeback_enabled(repo_full_name) or not head_sha:
        return False

    try:
        alerts = list_alerts_for_pr(repo_full_name, pr_number)
    except Exception:
        log.exception("could not read alerts to sync check: repo=%s pr=%s", repo_full_name, pr_number)
        return False

    unresolved = [a for a in alerts if a.status != "resolved"]
    blocking = [a for a in unresolved if a.severity == SEVERITY_BLOCKING]
    flagged = [a for a in unresolved if a.severity != SEVERITY_BLOCKING]

    if blocking:
        state = "failure"
        description = (f"{len(blocking)} finding{'s' if len(blocking) > 1 else ''} "
                       f"needs a decision before merge")
    elif flagged:
        # Deliberately green: a flagged finding never stopped the merge, so
        # reporting failure here would misrepresent what SHIP decided.
        state = "success"
        description = (f"{len(flagged)} finding{'s' if len(flagged) > 1 else ''} "
                       f"flagged for review — not blocking")
    else:
        state = "success"
        description = "No unresolved compliance findings"

    body = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
    if DASHBOARD_URL:
        body["target_url"] = DASHBOARD_URL

    return _post(
        f"{GITHUB_API_BASE}/repos/{repo_full_name}/statuses/{head_sha}",
        body,
        what="commit status",
    )
