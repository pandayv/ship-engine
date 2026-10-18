"""
One audit line per state change and per refusal in Relay.

Relay's tools decide things (a finding is allowed or blocked, a batch is refused) and the only
trace used to be the GitHub comment. These lines are what a person reads in CloudWatch
to answer "what happened to that decision": which tool, which pull request, which
findings, the outcome, and why it was refused.

They carry identifiers and a reason CODE only. Never the reason a person gave, never
finding text, never a secret. The Lambda request id is added so a line can be matched
with the rest of that invocation's logs.
"""

import logging

log = logging.getLogger("ship.relay.audit")

_request_id = ""


def set_request_id(request_id: str) -> None:
    """Called once per invocation by relay_lambda_handler. A module global is enough:
    a Lambda container serves one request at a time."""
    global _request_id
    _request_id = request_id or ""


def audit(tool: str, outcome: str, repo: str | None = None, pr_number: int | None = None,
          alert_ids=None, why: str | None = None, **counts) -> None:
    """outcome: "applied", "staged", "refused", "cancelled", "partial", ... `why` is a
    short machine-readable code for a refusal. `counts` are small integers (applied=2)."""
    pr = f"{repo}#{pr_number}" if repo is not None else "-"
    ids = ",".join(str(a)[:12] for a in (alert_ids or [])) or "-"
    extra = " ".join(f"{k}={v}" for k, v in counts.items())
    log.info("audit tool=%s outcome=%s pr=%s alerts=%s why=%s %s req=%s",
             tool, outcome, pr, ids, why or "-", extra, _request_id or "-")
