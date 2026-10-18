"""
Tracks when an alert was last shown on a screen — the signal
request_risk_acceptance needs to tell "reviewed, then confirmed" apart
from a blind decision, without which voice confirmation would be
indistinguishable from a rubber stamp. See src/relay/server.py's module
docstring for the full design reasoning behind why that distinction, not
"voice can never decide anything," is the actual rule.

WHY THIS IS KEYED BY ALERT, NOT BY SESSION — a real correction, not the
original design. The first version of this module keyed on the MCP
session id (Mcp-Session-Id), reasoning that it was a stable per-
conversation identifier. Verified live against the actual deployed
system, not assumed, and it wasn't: Relay's create_app() (see
src/relay/server.py) sets stateless_http=True, and for a real, load-
bearing reason — every Lambda invocation builds a brand-new FastMCP app
and session manager (the SnapStart-per-invocation fix), so there is no
process for a session id to have continuity WITH, whether or not the
protocol flag is set. A live test confirmed it directly: no
Mcp-Session-Id header ever arrived on the tool call following
initialize(). Session-scoping wasn't just unverified, it was structurally
impossible in this specific architecture.

The actual signal available on every request, with no transport
continuity required, is simpler: was THIS alert_id shown recently, at
all. TTL_SECONDS below (10 minutes) is that "recently" — long enough for
a real ask-review-decide exchange, short enough that a stale viewing from
an hour ago can't be invoked to unlock a much later, unrelated "go ahead."
This trades exact per-conversation attribution (which the transport can't
give us) for a time-windowed proxy that's a genuine, honest fit for what's
actually available, not a fiction of session continuity that isn't there.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-recent-reviews"
# Not ship-session-context (the name this table had under the rejected
# session-keyed design). Renamed rather than the old table's schema fixed
# in place — ship-agent's IAM identity deliberately has no
# dynamodb:DeleteTable, a properly withheld permission, and this name is
# also just more accurate for what the table actually holds now. The old,
# empty, wrong-schema table is harmless to leave orphaned (on-demand
# billing, zero items, zero cost).
RECENT_WINDOW_SECONDS = 600  # 10 minutes
# For a future DynamoDB TTL on `expires_at`; the window above is what the code enforces.
EXPIRY_SECONDS = 3600


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
        KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def mark_shown(alert_id: str, position: int | None = None) -> None:
    """Best-effort by design, same reasoning as repo_store.mark_event_seen():
    this is the UX gate, not the compliance record — a resolve_alert() call
    that follows this is what's durably written to ship-alerts regardless
    of whether this bookkeeping succeeds.

    `position` is the number the finding had on the screen that displayed it. A
    spoken "the second one" means the number the person SAW, and live positions shift
    when findings arrive or are decided, so the displayed number is kept with the
    record of having been shown (see findings_routing in src/relay/server.py)."""
    now = int(time.time())
    item = {"alert_id": alert_id, "shown_at": now, "expires_at": now + EXPIRY_SECONDS}
    if position is not None:
        item["position"] = int(position)
    try:
        _table().put_item(Item=item)
    except Exception:
        log.warning("could not record %s as shown", alert_id, exc_info=True)


def recent_view(alert_id: str) -> dict | None:
    """{"shown_at": int, "position": int | None} if this alert was shown within the
    window, else None. Fails closed, same reasoning as repo_store.is_watched()'s
    SECURITY NOTE: a datastore error here must read as "not reviewed," never as
    "reviewed" — the failure mode that matters is a blind decision slipping
    through, not an extra trip to Gate. The read is strongly consistent: a view
    recorded a moment ago must count."""
    try:
        item = _table().get_item(Key={"alert_id": alert_id}, ConsistentRead=True).get("Item")
        if not item:
            return None
        shown_at = int(item["shown_at"])
        if (int(time.time()) - shown_at) > RECENT_WINDOW_SECONDS:
            return None
        position = item.get("position")
        return {"shown_at": shown_at, "position": int(position) if position is not None else None}
    except Exception:
        log.warning("could not check whether %s was shown recently — treating as not reviewed",
                    alert_id, exc_info=True)
        return None


def was_shown_recently(alert_id: str) -> bool:
    return recent_view(alert_id) is not None
