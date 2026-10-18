"""
Lambda entrypoint for ship-device-gateway — the WebSocket API's $connect,
$disconnect, and register routes.

This is deliberately a separate, tiny Lambda from Relay. Relay's own IAM
role is scoped to read-only on exactly two DynamoDB tables (see
src/relay/server.py's docstring) precisely so it can never do more than
answer a question — connection-table writes are a different, narrow
responsibility, not something worth widening Relay's own blast radius
for. This function does exactly three things and nothing else: record a
connection, remove one, and let a display page name itself.

No business logic lives here. It never reads alerts, never talks to
Detector or Triage — it is pure plumbing for src/storage/device_store.py,
the same "Relay carries no judgment" principle applied to the connection
registry instead of the MCP tools.

ACK ON REGISTER, EXPLAINED. A WebSocket route's Lambda return value is
NOT relayed back to the client by API Gateway unless a route response is
separately configured (confirmed against AWS's own docs before writing
this, not assumed) — proxy integration here is one-way. Rather than add
that second, different two-way-communication mechanism just for this one
ack, register explicitly posts its own confirmation back over the same
Management API path push.py already uses for everything else — one
mechanism for every message this system ever sends a client, not two.
The endpoint is built from the event's own domainName/stage rather than
a hardcoded config value, since both are already present on every
WebSocket invocation and self-describing beats a second source of truth.
"""

import json
import logging

from src.storage import push_store, session_store
from src.storage.device_store import register_connection, remove_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)


def _management_endpoint(event) -> str:
    ctx = event.get("requestContext", {})
    return f"https://{ctx['domainName']}/{ctx['stage']}"


def _ack(event, connection_id: str, body: dict) -> None:
    import boto3

    client = boto3.client("apigatewaymanagementapi", endpoint_url=_management_endpoint(event))
    client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(body).encode("utf-8"))


def handler(event, context):
    route = event.get("requestContext", {}).get("routeKey", "")
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    if route == "$connect":
        # Registration happens on the explicit 'register' route, not here
        # — $connect fires before the client has had a chance to send its
        # chosen device name, so there is nothing to store yet.
        log.info("connect: connection_id=%s", connection_id)
        return {"statusCode": 200}

    if route == "$disconnect":
        remove_connection(connection_id)
        log.info("disconnect: connection_id=%s", connection_id)
        return {"statusCode": 200}

    if route == "subscribe":
        body = json.loads(event.get("body") or "{}")
        device_name = (body.get("device_name") or "").strip()
        try:
            push_store.save_subscription(device_name, body.get("subscription") or {})
        except ValueError as exc:
            return {"statusCode": 400, "body": str(exc)}
        log.info("subscribe: device_name=%s", device_name)
        try:
            _ack(event, connection_id, {"subscribed": device_name.lower()})
        except Exception:
            log.exception("could not confirm the subscription to connection %s", connection_id)
        return {"statusCode": 200}

    if route == "ack":
        # The page says it displayed the findings Relay was holding for it. Only now do
        # they count as seen, and only the ones that were actually being held.
        body = json.loads(event.get("body") or "{}")
        device_name = (body.get("device_name") or "").strip()
        waiting = push_store.get_pending(device_name) if device_name else None
        if waiting:
            held = set(waiting["alert_ids"])
            for alert_id in body.get("alert_ids") or []:
                if alert_id in held:
                    session_store.mark_shown(alert_id)
            push_store.clear_pending(device_name)
            log.info("ack: %s displayed %d held findings", device_name, len(held))
        return {"statusCode": 200}

    if route == "register":
        body = json.loads(event.get("body") or "{}")
        device_name = (body.get("device_name") or "").strip()
        if not device_name:
            return {"statusCode": 400, "body": "device_name is required"}
        register_connection(connection_id, device_name)
        log.info("register: connection_id=%s device_name=%s", connection_id, device_name)
        try:
            _ack(event, connection_id, {"registered": device_name.lower()})
        except Exception:
            # The registration itself already succeeded and is durably
            # stored — a push can still reach this device later even if
            # this one confirmation never arrives. Log it, don't fail the
            # route over it.
            log.exception("could not send register ack to connection_id=%s", connection_id)

        # Findings held for this screen while its page was closed (see src/relay/server.py,
        # _notify_registered_device). The page renders them and then sends "ack".
        try:
            waiting = push_store.get_pending(device_name)
            if waiting:
                _ack(event, connection_id, waiting["payload"])
                log.info("delivered held findings to %s", device_name)
        except Exception:
            log.exception("could not deliver held findings to connection_id=%s", connection_id)
        return {"statusCode": 200}

    log.warning("unrecognized route: %s", route)
    return {"statusCode": 400, "body": "unrecognized route"}
