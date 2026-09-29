"""
Tracks when an alert was last shown on a screen — the signal
request_risk_acceptance needs to tell "reviewed, then confirmed" apart
from "blind approval," without which voice confirmation would be
indistinguishable from a rubber stamp. See src/relay/server.py's module
docstring for the full design reasoning behind why that distinction, not
"voice can never approve anything," is the actual rule.

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


def mark_shown(alert_id: str) -> None:
    """Best-effort by design, same reasoning as repo_store.mark_event_seen():
    this is the UX gate, not the compliance record — a resolve_alert() call
    that follows this is what's durably written to ship-alerts regardless
    of whether this bookkeeping succeeds."""
    try:
        _table().put_item(Item={"alert_id": alert_id, "shown_at": int(time.time())})
    except Exception:
        log.warning("could not record %s as shown", alert_id)


def was_shown_recently(alert_id: str) -> bool:
    """Fails closed, same reasoning as repo_store.is_watched()'s SECURITY
    NOTE: a datastore error here must read as "not reviewed," never as
    "reviewed" — the failure mode that matters is a blind approval slipping
    through, not an extra trip to Gate."""
    try:
        item = _table().get_item(Key={"alert_id": alert_id}).get("Item")
        if not item:
            return False
        return (int(time.time()) - int(item["shown_at"])) <= RECENT_WINDOW_SECONDS
    except Exception:
        log.warning("could not check whether %s was shown recently — treating as not reviewed", alert_id)
        return False
