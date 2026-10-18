"""
Which devices are currently reachable for a push, and under what name.

WHY PUSH, NOT POLL. A real deployment fires a release maybe weekly and a
genuine compliance finding far less often than that — a display device
polling every few seconds to catch an event that happens roughly once a
month spends almost all of that traffic finding nothing changed. An idle
WebSocket connection costs nothing while nothing is happening and delivers
a push the instant something does. This store exists to make that
possible: it is the address book letting `display_on` (src/relay/server.py)
find the one specific connection to push to, by the name a human asked
for ("show it on the iPad") — not a way to keep every device in sync,
which this system was never trying to do.

ANY device with a browser and an open WebSocket connection can register
here — a TV's browser, an iPad, a laptop, a smart-fridge display. Nothing
here is device-specific; the "device" is just whatever name a display
page chose to register under.

Rows are inherently short-lived: API Gateway WebSocket connections drop
on their own (idle timeout, browser tab closed, network blip), and the
$disconnect route removes the row when that's caught — but a disconnect
notification is not guaranteed to fire (a hard network cut, a crashed
tab). So every push MUST treat a stale connection_id as an expected,
routine case, not an error — see push_to_device() below.
"""

import logging
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-device-connections"

# API Gateway closes a WebSocket connection after 2 hours whatever happens, and after
# 10 idle minutes. A $disconnect that never fires leaves a row behind, and no TTL is
# enabled on the table yet, so a row is treated as dead once either limit has passed.
# The display page re-registers every ~5 minutes, which is what keeps last_seen_at
# fresh on a healthy connection; the idle margin sits above the 10 minute limit.
CONNECTION_HARD_CAP_SECONDS = 2 * 3600
CONNECTION_IDLE_SECONDS = 15 * 60
# For a future DynamoDB TTL on `expires_at` (not enabled by this code).
ROW_EXPIRY_SECONDS = CONNECTION_HARD_CAP_SECONDS + 3600


@dataclass
class DeviceConnection:
    connection_id: str
    device_name: str
    connected_at: str
    last_seen_at: str = ""
    expires_at: int = 0


_KNOWN = {f.name for f in fields(DeviceConnection)}


def _row(item: dict) -> DeviceConnection:
    """Attributes a newer writer added are ignored rather than crashing an older reader."""
    row = {k: v for k, v in item.items() if k in _KNOWN}
    if "expires_at" in row:
        row["expires_at"] = int(row["expires_at"])
    return DeviceConnection(**row)


def _age_seconds(iso: str, now: float) -> float | None:
    try:
        return now - datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def is_live(conn: DeviceConnection, now: float | None = None) -> bool:
    """False once the connection is past API Gateway's hard cap or has been silent
    longer than its idle limit. A row with an unreadable timestamp is kept: when
    unsure, a push attempt (and its Gone handling) is the cheaper mistake."""
    now = time.time() if now is None else now
    age = _age_seconds(conn.connected_at, now)
    if age is not None and age > CONNECTION_HARD_CAP_SECONDS:
        return False
    idle = _age_seconds(conn.last_seen_at or conn.connected_at, now)
    return not (idle is not None and idle > CONNECTION_IDLE_SECONDS)


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
        KeySchema=[{"AttributeName": "connection_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "connection_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def register_connection(connection_id: str, device_name: str) -> None:
    """Called from the WebSocket 'register' route once a display page has
    named itself, and again every few minutes by the same page (idempotent: it
    refreshes last_seen_at and keeps connected_at from the first call). Deliberately
    does NOT remove a prior row under the same device_name from a different
    connection — if two tabs register as 'tv', both are valid push targets (see
    list_connections_for_device)."""
    now = datetime.now(timezone.utc)
    _table().update_item(
        Key={"connection_id": connection_id},
        UpdateExpression=("SET device_name = :d, last_seen_at = :now, expires_at = :exp, "
                          "connected_at = if_not_exists(connected_at, :now)"),
        ExpressionAttributeValues={
            ":d": device_name.strip().lower(), ":now": now.isoformat(),
            ":exp": int(now.timestamp()) + ROW_EXPIRY_SECONDS,
        },
    )


def remove_connection(connection_id: str) -> None:
    """Called from $disconnect, and from push_to_device() when a push
    discovers a connection is already gone. Both call sites are routine,
    not error paths — see this module's docstring."""
    _table().delete_item(Key={"connection_id": connection_id})


def list_connections_for_device(device_name: str) -> list[DeviceConnection]:
    """A Scan with a filter, not a Query — device_name isn't this table's
    key, only connection_id is. Correct and cheap at the scale this runs
    at (a handful of devices in a single household/demo); a GSI on
    device_name is the move if that ever stops being true, not before."""
    device_name = device_name.strip().lower()
    items, kwargs = [], {
        "FilterExpression": "device_name = :d",
        "ExpressionAttributeValues": {":d": device_name},
    }
    table = _table()
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return [c for c in (_row(i) for i in items) if is_live(c)]


def list_all_connections() -> list[DeviceConnection]:
    items, kwargs = [], {}
    table = _table()
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return [c for c in (_row(i) for i in items) if is_live(c)]
