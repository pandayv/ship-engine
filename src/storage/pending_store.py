"""
Staged, not-yet-committed voice resolutions for one PR at a time — see
docs/voice-resolution-workflow.md for the full design.

A multi-finding voice turn ("first one's a non-issue, second one's an
actual blocker") doesn't resolve anything immediately. It writes what was
captured here, pushes it to the screen as pending, and waits for an
explicit "proceed" before anything in ship-alerts actually changes. This
is what makes the design safe without a spoken read-back and without an
undo mechanism: nothing is final until proceed commits it, so a mistake
caught on the pending screen is a correction before commit, not an undo
after one.

Keyed by repo#pr_number, not by alert_id or session — the workflow doc's
own scoping decision (voice resolves one PR's findings at a time, bulk
and ordinal commands don't reach across PRs). A second "stage" call for
the same PR overwrites whatever was pending before; there's exactly one
pending set per PR, not a history of them.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-pending-dispositions"
STALE_AFTER_SECONDS = 600  # same window session_store uses for "recently shown"


def _key(repo: str, pr_number: int) -> str:
    return f"{repo}#{pr_number}"


@lru_cache(maxsize=1)
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(TABLE_NAME)


def create_table_if_not_exists() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    try:
        client.describe_table(TableName=TABLE_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pr_key", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pr_key", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def stage(repo: str, pr_number: int, decisions: list[dict]) -> None:
    """decisions: list of {"position": int, "alert_id": str, "approved":
    bool, "reason": str}. Overwrites any previously staged set for this
    PR — there's one pending batch per PR, not an accumulating queue."""
    _table().put_item(Item={
        "pr_key": _key(repo, pr_number),
        "staged_at": int(time.time()),
        "decisions": decisions,
    })


def get_pending(repo: str, pr_number: int) -> list[dict] | None:
    """None if nothing is staged, or if what's staged is stale (someone
    said "first one's fine" and then never said proceed — ten minutes
    later that context shouldn't still be live). Fails closed, same
    reasoning as session_store.was_shown_recently(): a datastore error
    must read as "nothing pending," never silently resolve something
    that was never actually confirmed."""
    try:
        item = _table().get_item(Key={"pr_key": _key(repo, pr_number)}).get("Item")
        if not item:
            return None
        if (int(time.time()) - int(item["staged_at"])) > STALE_AFTER_SECONDS:
            return None
        return item["decisions"]
    except Exception:
        log.warning("could not read pending disposition for %s#%s", repo, pr_number)
        return None


def clear_pending(repo: str, pr_number: int) -> None:
    """Best-effort, same reasoning as session_store.mark_shown(): this is
    bookkeeping, not the compliance record. If this fails, the next stage
    call overwrites the stale entry anyway (see stage())."""
    try:
        _table().delete_item(Key={"pr_key": _key(repo, pr_number)})
    except Exception:
        log.warning("could not clear pending disposition for %s#%s", repo, pr_number)
