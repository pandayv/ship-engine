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
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-device-connections"


@dataclass
class DeviceConnection:
    connection_id: str
    device_name: str
    connected_at: str


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
    named itself. Overwrites any previous row for this connection_id, and
    deliberately does NOT remove a prior row under the same device_name
    from a different connection — if two tabs register as 'tv', both are
    valid push targets (see list_connections_for_device)."""
    _table().put_item(Item={
        "connection_id": connection_id,
        "device_name": device_name.strip().lower(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    })


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
    return [DeviceConnection(**i) for i in items]


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
    return [DeviceConnection(**i) for i in items]
