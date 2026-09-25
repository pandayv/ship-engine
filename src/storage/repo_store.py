"""
The repositories SHIP watches.

This used to be SHIP_ALLOWED_REPOS, a comma-separated environment variable
on both Lambdas. That was wrong in two ways that mattered. Practically:
Lambda caps all environment variables at 4 KB combined, so the list topped
out somewhere around sixty repositories and could not grow, and adding one
meant a configuration update on two functions kept in sync by hand — a
deploy, to onboard a customer. Conceptually: a product where connecting a
repository is a backend chore is not a product.

Repositories live in their own table rather than sharing ship-alerts
because the access patterns have nothing in common. Alerts are keyed by
alert_id and read as a queue; repositories are keyed by full name and read
as a point lookup on the hot path of every webhook delivery. Partitioning
on repo_full_name makes the allowlist check a single GetItem — constant
time whether ten repositories are connected or ten thousand — instead of
parsing a string that grows without bound.

SECURITY NOTE, deliberately load-bearing. is_watched() is the control that
stops an arbitrary webhook payload naming someone else's repository and
spending this service's GitHub token and Bedrock quota against it (review
finding #7). It therefore FAILS CLOSED: if DynamoDB cannot be read, the
answer is whatever the environment-variable bootstrap says and nothing
more — never "allow". A convenient fallback that admits everything when
the datastore hiccups would quietly delete the control it is standing in
for.
"""

import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-repos"

STATUS_AWAITING = "awaiting_first_event"  # connected here, webhook not yet delivered
STATUS_WATCHING = "watching"              # a real delivery has arrived

# Kept as a bootstrap path, not a legacy wart: it lets local development
# and the test suite run with no AWS at all, and it means an existing
# deployment keeps working through the switch to table-backed storage
# without a migration step.
BOOTSTRAP_REPOS = tuple(
    r.strip() for r in os.environ.get("SHIP_ALLOWED_REPOS", "").split(",") if r.strip()
)


@dataclass
class WatchedRepo:
    repo_full_name: str
    status: str
    connected_at: str
    last_event_at: str | None = None
    connected_by: str = ""


@lru_cache(maxsize=1)
def _table():
    import boto3  # lazy, same reasoning as alert_store

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
        KeySchema=[{"AttributeName": "repo_full_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "repo_full_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def _row_to_repo(item: dict) -> WatchedRepo:
    item = dict(item)
    item.setdefault("last_event_at", None)
    item.setdefault("connected_by", "")
    return WatchedRepo(**item)


def connect_repo(repo_full_name: str, connected_by: str = "") -> WatchedRepo:
    """
    Idempotent: reconnecting an already-connected repository must not reset
    its status back to awaiting, which would make a live integration look
    broken in the dashboard.
    """
    existing = get_repo(repo_full_name)
    if existing is not None:
        return existing

    repo = WatchedRepo(
        repo_full_name=repo_full_name,
        status=STATUS_AWAITING,
        connected_at=datetime.now(timezone.utc).isoformat(),
        connected_by=connected_by,
    )
    _table().put_item(Item={k: v for k, v in asdict(repo).items() if v is not None})
    return repo


def disconnect_repo(repo_full_name: str) -> None:
    _table().delete_item(Key={"repo_full_name": repo_full_name})


def get_repo(repo_full_name: str) -> WatchedRepo | None:
    item = _table().get_item(Key={"repo_full_name": repo_full_name}).get("Item")
    return _row_to_repo(item) if item else None


def list_repos() -> list[WatchedRepo]:
    table = _table()
    items, kwargs = [], {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return sorted((_row_to_repo(i) for i in items), key=lambda r: r.connected_at)


def is_watched(repo_full_name: str) -> bool:
    """
    The allowlist check on the webhook hot path. One GetItem, so it stays
    constant-time as the number of connected repositories grows.

    Fails closed on a datastore error — see this module's SECURITY NOTE.
    """
    if repo_full_name in BOOTSTRAP_REPOS:
        return True
    try:
        return get_repo(repo_full_name) is not None
    except Exception:
        log.exception(
            "could not read %s to check whether %r is connected — denying, "
            "since allowing on a datastore error would defeat the allowlist",
            TABLE_NAME, repo_full_name,
        )
        return False


def mark_event_seen(repo_full_name: str) -> None:
    """
    Flips a repository from awaiting to watching on its first real webhook
    delivery, and records the most recent one.

    Written as a conditional update on an existing row so that a delivery
    for a repository allowed only by the environment bootstrap does not
    silently create a table row — the bootstrap list and the connected
    list stay distinct, rather than one quietly migrating into the other.

    Best-effort by design: this is dashboard presentation, and failing to
    update it must never fail the delivery it is describing.
    """
    try:
        _table().update_item(
            Key={"repo_full_name": repo_full_name},
            UpdateExpression="SET #s = :watching, last_event_at = :now",
            ConditionExpression="attribute_exists(repo_full_name)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":watching": STATUS_WATCHING,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        if type(e).__name__ != "ConditionalCheckFailedException":
            log.warning("could not record webhook activity for %s: %s", repo_full_name, e)
