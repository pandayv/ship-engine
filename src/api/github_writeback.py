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

So no caller ever states a verdict directly. sync_pr_check() recomputes
it from DynamoDB — the shared source of truth — and posts what it derives.
Every writer computes from the same data, so concurrent writes converge on
the same answer instead of fighting, ordering stops mattering, and an
intermediate state is still truthful ("2 findings blocking, so far"). The
same function serves the human-decision path, so resolving the last
blocking alert flips the check green through exactly the code that turned
it red.

WHAT A DECISION DOES TO THE STATUS. derive_commit_status() is the one place
that maps stored alerts to a state:
  - a finding waiting for a decision blocks if its severity is blocking
  - approve: the person accepted the risk, the finding stops blocking
  - reject: the finding is real, the PR stays blocked (failure) until a later
    commit no longer contains the flagged fragment (alert_store.is_held and
    release_fixed_rejections)
Only the lifecycle statuses (pending while a review runs, error when one could
not run) are posted by post_commit_status() directly; they carry no verdict.

WHICH COMMIT. A status belongs to a SHA. A decision can arrive long after the
commit the finding was found on, and a status posted there leaves the PR's
current head with a stale (often red, sometimes green) status. So the status
always goes to the PR's CURRENT head, fetched from GitHub; the SHA stored on the
alert is only the fallback if that lookup fails.

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
from dataclasses import dataclass

import requests

from src.storage.alert_store import SEVERITY_BLOCKING, is_held, is_open, list_alerts_for_pr

log = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
STATUS_CONTEXT = "ship/compliance"
REQUEST_TIMEOUT = 10

# Set to the Gate dashboard's public URL so a status check links
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


def _get_json(url: str, what: str) -> dict | None:
    try:
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {_token()}",
                "Accept": "application/vnd.github+json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as e:
        log.warning("GitHub lookup failed (%s): %s — url=%s", what, e, url)
        return None


def current_head_sha(repo_full_name: str, pr_number: int, fallback: str = "") -> str:
    """The commit the pull request points at right now. Falls back to `fallback`
    (the SHA stored on the alert) with a warning if GitHub cannot be asked, so a
    brief outage degrades to the old behavior instead of posting nothing."""
    pull = _get_json(f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}", what="pull request head")
    sha = ((pull or {}).get("head") or {}).get("sha", "")
    if sha:
        return sha
    log.warning("could not read the current head of %s#%s, using the stored sha %r", repo_full_name, pr_number, fallback)
    return fallback


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
    """accepted=True is "approve": the risk is accepted. accepted=False is
    "reject": the finding is real and the PR stays blocked until the code is fixed.
    Whether anything ELSE still blocks is the status's business, so the approve
    wording is conditional ("if nothing else is blocking")."""
    heading = ("### Risk accepted" if accepted
               else "### Rejected — this needs a code fix")
    outcome = ("This finding no longer blocks the merge. SHIP's check passes once nothing else is blocking."
               if accepted else
               "SHIP's check stays failing and the merge stays blocked until a commit fixes this code.")
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


@dataclass(frozen=True)
class CommitStatus:
    state: str          # "success" | "failure"
    description: str


def _plural(n: int, noun: str = "finding") -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def derive_commit_status(alerts) -> CommitStatus:
    """The ONE mapping from stored alerts to the PR's commit status. Used by every
    path that posts a verdict (webhook, fragment processor, Gate, Relay's voice
    decisions), so none of them can drift on what a decision means:

      waiting for a decision, blocking severity   -> failure
      rejected, flagged code still in the PR       -> failure
      waiting for a decision, review severity      -> success (flagged, not blocking)
      approved (risk accepted), or nothing         -> success
    """
    waiting = [a for a in alerts if is_open(a) and a.severity == SEVERITY_BLOCKING]
    held = [a for a in alerts if is_held(a)]
    flagged = [a for a in alerts if is_open(a) and a.severity != SEVERITY_BLOCKING]

    if waiting or held:
        parts = []
        if waiting:
            parts.append(f"{_plural(len(waiting))} {'needs' if len(waiting) == 1 else 'need'} a decision")
        if held:
            parts.append(f"{_plural(len(held))} rejected, fix the code")
        return CommitStatus("failure", "; ".join(parts))
    if flagged:
        # Deliberately green: a flagged finding never stopped the merge, so
        # reporting failure here would misrepresent what SHIP decided.
        return CommitStatus("success", f"{_plural(len(flagged))} flagged for review — not blocking")
    return CommitStatus("success", "No unresolved compliance findings")


def post_commit_status(repo_full_name: str, pr_number: int, state: str, description: str,
                       head_sha: str = "", trust_head: bool = False) -> bool:
    """Posts one `ship/compliance` status to the PR's current head. `state` is one of
    pending / success / failure / error. `head_sha` is the fallback (or, with
    trust_head, the commit to use as is: the webhook has just been told the head by
    GitHub itself, so asking again would only add a round trip)."""
    if not _writeback_enabled(repo_full_name):
        return False
    sha = head_sha if (trust_head and head_sha) else current_head_sha(repo_full_name, pr_number, head_sha)
    if not sha:
        log.error("no commit to post the %s status to: repo=%s pr=%s", state, repo_full_name, pr_number)
        return False
    body = {"state": state, "context": STATUS_CONTEXT, "description": description[:140]}
    if DASHBOARD_URL:
        body["target_url"] = DASHBOARD_URL
    return _post(f"{GITHUB_API_BASE}/repos/{repo_full_name}/statuses/{sha}", body, what=f"commit status {state}")


def sync_pr_check(repo_full_name: str, pr_number: int, head_sha: str = "", final: bool = True, alerts=None) -> bool:
    """
    Recomputes the commit status from stored alerts and posts it to the PR's
    current head. Safe to call from any number of concurrent fragments and from
    the dashboard — see this module's RACE SAFETY note for why that holds.

    final=False means "other fragments of this review are still running": a
    failure is still posted at once (it is already true), but an otherwise-green
    result stays pending, so a required check cannot be satisfied before every
    fragment has reported. `alerts` lets a caller that has just read the gate
    alerts reuse that read.
    """
    if not _writeback_enabled(repo_full_name):
        return False

    if alerts is None:
        try:
            alerts = list_alerts_for_pr(repo_full_name, pr_number)
        except Exception:
            log.exception("could not read alerts to sync check: repo=%s pr=%s", repo_full_name, pr_number)
            return False
    else:
        alerts = [a for a in alerts if a.repo == repo_full_name and a.pr_number == pr_number]

    status = derive_commit_status(alerts)
    if status.state == "success" and not final:
        return post_commit_status(repo_full_name, pr_number, "pending",
                                  "SHIP is still reviewing this commit", head_sha)
    return post_commit_status(repo_full_name, pr_number, status.state, status.description, head_sha)
