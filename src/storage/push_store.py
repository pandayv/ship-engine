"""
Devices that can be reached by a push notification even when no page is open,
and the findings waiting for them.

Two small tables:

  ship-push-subscriptions   one row per browser that agreed to alerts: its push
                            address (the browser's own subscription) and the name
                            the person gave that screen ("tablet").
  ship-pending-display      the findings Relay wants a named screen to show, held
                            for ten minutes. When the person taps the
                            notification and the page connects, it receives these.

A notification is not the same as having seen the findings. The voice gate counts
a finding as shown only when the page has displayed it and says so (the "ack"
route in device_gateway_handler.py), and only for findings in the pending set,
so a stray message cannot mark arbitrary findings as seen.
"""

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger(__name__)

SUBSCRIPTIONS_TABLE = "ship-push-subscriptions"
PENDING_TABLE = "ship-pending-display"
PENDING_TTL_SECONDS = 600


def _resource():
    import boto3

    return boto3.resource("dynamodb")


@lru_cache(maxsize=1)
def _subscriptions():
    return _resource().Table(SUBSCRIPTIONS_TABLE)


@lru_cache(maxsize=1)
def _pending():
    return _resource().Table(PENDING_TABLE)


def create_tables_if_not_exist() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    for name, key in ((SUBSCRIPTIONS_TABLE, "endpoint_hash"), (PENDING_TABLE, "device_name")):
        try:
            client.describe_table(TableName=name)
            continue
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        client.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.get_waiter("table_exists").wait(TableName=name)


def _endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:32]


# --- subscriptions ---


def save_subscription(device_name: str, subscription: dict) -> None:
    """Stores (or refreshes) a browser's push address under a screen name. The same browser
    registering again, even under a new name, replaces its own row."""
    endpoint = (subscription or {}).get("endpoint")
    if not endpoint or not (subscription.get("keys") or {}).get("p256dh"):
        raise ValueError("not a push subscription: it needs an endpoint and keys")
    _subscriptions().put_item(Item={
        "endpoint_hash": _endpoint_hash(endpoint),
        "device_name": device_name.strip().lower(),
        "subscription": json.dumps(subscription),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def _scan_subscriptions() -> list[dict]:
    items, kwargs = [], {}
    while True:
        page = _subscriptions().scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def subscriptions_for_device(device_name: str) -> list[dict]:
    """[{endpoint_hash, subscription}] for one screen name. A handful of rows, so a scan."""
    wanted = device_name.strip().lower()
    return [
        {"endpoint_hash": i["endpoint_hash"], "subscription": json.loads(i["subscription"])}
        for i in _scan_subscriptions() if i["device_name"] == wanted
    ]


def subscribed_device_names() -> set[str]:
    return {i["device_name"] for i in _scan_subscriptions()}


def remove_subscription(endpoint_hash: str) -> None:
    _subscriptions().delete_item(Key={"endpoint_hash": endpoint_hash})


# --- findings waiting for a screen ---


def save_pending(device_name: str, payload: dict, alert_ids: list[str]) -> None:
    _pending().put_item(Item={
        "device_name": device_name.strip().lower(),
        "payload": json.dumps(payload),
        "alert_ids": list(alert_ids),
        "expires_at": int(time.time()) + PENDING_TTL_SECONDS,
    })


def get_pending(device_name: str) -> dict | None:
    """{payload, alert_ids}, or None if nothing is waiting or it has gone stale."""
    item = _pending().get_item(Key={"device_name": device_name.strip().lower()}).get("Item")
    if not item or int(item.get("expires_at", 0)) < time.time():
        return None
    return {"payload": json.loads(item["payload"]), "alert_ids": list(item.get("alert_ids", []))}


def clear_pending(device_name: str) -> None:
    _pending().delete_item(Key={"device_name": device_name.strip().lower()})


if __name__ == "__main__":
    create_tables_if_not_exist()
    print(f"{SUBSCRIPTIONS_TABLE} and {PENDING_TABLE} are ready")
