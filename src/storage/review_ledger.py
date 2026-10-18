"""
Which fragments of a commit's review have reported, so the PR's check only turns
green when every one of them has.

A PR is reviewed as independent fragments, each its own SQS job (see src/api/main.py).
No fragment knows about the others, and a fragment that finds nothing leaves no trace
in the alerts table. Posting "all clear" whenever one fragment finishes cleanly would
turn a required check green while a sibling is still being judged, so the webhook
records what it queued here and each fragment ticks itself off.

One item per pull request in the existing ship-status table (key `summary_id`, the
release summary lives under a different key), so there is no new table:

    summary_id    "review#<repo>#<pr>"
    head_sha      the commit this review is for
    expected      string set of fragment keys the review must hear back from
    done          string set of the keys that have reported (a set, so an SQS
                  redelivery counts once)
    expires_at    epoch seconds, for a future DynamoDB TTL

A fragment that never reports (the queue gave up on it, or it was too large to queue)
keeps its key in `expected` and so the check never turns green by itself. That is the
point: the absence of a verdict must not read as a pass. A dead-letter consumer or a
reconciler would be the real fix for those (see the follow-ups in the change notes).
"""

from __future__ import annotations

import logging
import time

from src.storage import status_store

log = logging.getLogger(__name__)

LEDGER_TTL_SECONDS = 7 * 24 * 3600


def _key(repo: str, pr_number: int) -> str:
    return f"review#{repo}#{pr_number}"


def begin_review(repo: str, pr_number: int, head_sha: str, expected_keys: list[str]) -> bool:
    """Records the review the webhook is about to queue. Replaces any earlier one for
    this PR: a new commit starts a new review. False (and an error log) if it could not
    be written; the fragments then fall back to reporting as they finish."""
    if not expected_keys or not head_sha:
        return False
    try:
        status_store._table().put_item(Item={
            "summary_id": _key(repo, pr_number),
            "head_sha": head_sha,
            "expected": set(expected_keys),
            "expires_at": int(time.time()) + LEDGER_TTL_SECONDS,
        })
        return True
    except Exception:
        log.exception("could not record the review ledger for %s#%s", repo, pr_number)
        return False


def fragment_reported(repo: str, pr_number: int, head_sha: str, fragment_key: str) -> bool:
    """Ticks one fragment off. True when the review it belongs to is complete, so the
    caller may post a green result. False while siblings are still outstanding, or when
    the fragment belongs to an older commit than the current review.

    No ledger at all (the sequential local path, a message queued before this existed,
    a ledger that could not be written) counts as complete: that is the behavior the
    system had before, and the alternative is a check that never finishes. Raises on a
    datastore error, so the caller can keep the check pending and say why."""
    table = status_store._table()
    key = {"summary_id": _key(repo, pr_number)}
    item = table.get_item(Key=key, ConsistentRead=True).get("Item")
    if not item:
        return True
    if item.get("head_sha") != head_sha:
        return False        # a newer commit started its own review; this result is for an old one
    try:
        response = table.update_item(
            Key=key,
            UpdateExpression="ADD done :k",
            ConditionExpression="head_sha = :h",
            ExpressionAttributeValues={":k": {fragment_key}, ":h": head_sha},
            ReturnValues="ALL_NEW",
        )
    except Exception as e:
        if type(e).__name__ == "ConditionalCheckFailedException":
            return False
        raise
    attributes = response["Attributes"]
    return set(attributes.get("expected", set())) <= set(attributes.get("done", set()))
