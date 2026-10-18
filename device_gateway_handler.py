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
import re

from src.storage import push_store, session_store
from src.storage.device_store import register_connection, remove_connection

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)

# A screen name is spoken back by Alexa ("Putting it on your tablet"), so it stays short and plain.
DEVICE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,29}$")


def _management_endpoint(event) -> str:
    ctx = event.get("requestContext", {})
    return f"https://{ctx['domainName']}/{ctx['stage']}"


def _ack(event, connection_id: str, body: dict) -> None:
    import boto3

    client = boto3.client("apigatewaymanagementapi", endpoint_url=_management_endpoint(event))
    client.post_to_connection(ConnectionId=connection_id, Data=json.dumps(body).encode("utf-8"))


def _body(event) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _reject(event, connection_id: str, action: str, message: str) -> dict:
    """The proxy integration cannot return a status to the browser (see the module docstring),
    so a refusal is a message on the connection. The page shows it; the route still reports
    400 for the logs."""
    log.warning("%s refused: %s (connection_id=%s)", action, message, connection_id)
    try:
        _ack(event, connection_id, {"type": "error", "action": action, "error": message})
    except Exception:
        log.exception("could not send the %s refusal to connection_id=%s", action, connection_id)
    return {"statusCode": 400, "body": message}


def _displayed_positions(payload: dict) -> dict:
    """alert_id -> the number it carried on the held screen, so a finding acknowledged by a
    page counts as shown with the number the person actually saw."""
    positions = {}
    for pr in (payload or {}).get("pull_requests", []):
        for f in pr.get("findings", []):
            if f.get("alert_id") is not None and isinstance(f.get("position"), int):
                positions[f["alert_id"]] = f["position"]
    return positions


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
        body = _body(event)
        device_name = str(body.get("device_name") or "").strip()
        if not DEVICE_NAME_PATTERN.match(device_name):
            return _reject(event, connection_id, "subscribe", "A screen name of 1 to 30 letters or digits is required.")
        try:
            push_store.save_subscription(device_name, body.get("subscription") or {})
        except ValueError as exc:
            return _reject(event, connection_id, "subscribe", str(exc))
        log.info("subscribe: device_name=%s", device_name)
        try:
            _ack(event, connection_id, {"type": "subscribed", "subscribed": device_name.lower()})
        except Exception:
            log.exception("could not confirm the subscription to connection %s", connection_id)
        return {"statusCode": 200}

    if route == "ack":
        # The page says it displayed the findings Relay was holding for it, while visible.
        # Only now do they count as seen, and only the ones that were actually being held.
        body = _body(event)
        device_name = str(body.get("device_name") or "").strip()
        waiting = push_store.get_pending(device_name) if device_name else None
        if waiting:
            held = set(waiting["alert_ids"])
            positions = _displayed_positions(waiting["payload"])
            acked = [a for a in (body.get("alert_ids") or []) if a in held]
            for alert_id in acked:
                session_store.mark_shown(alert_id, position=positions.get(alert_id))
            push_store.clear_pending(device_name)
            log.info("ack: %s displayed %d of %d held findings", device_name, len(acked), len(held))
        return {"statusCode": 200}

    if route == "register":
        body = _body(event)
        device_name = str(body.get("device_name") or "").strip()
        if not DEVICE_NAME_PATTERN.match(device_name):
            return _reject(event, connection_id, "register", "A screen name of 1 to 30 letters or digits is required.")
        register_connection(connection_id, device_name)
        log.info("register: connection_id=%s device_name=%s", connection_id, device_name)
        try:
            _ack(event, connection_id, {"type": "registered", "registered": device_name.lower()})
        except Exception:
            # The registration itself already succeeded and is durably
            # stored — a push can still reach this device later even if
            # this one confirmation never arrives. Log it, don't fail the
            # route over it.
            log.exception("could not send register ack to connection_id=%s", connection_id)

        # Findings held for this screen while its page was closed (see src/relay/server.py,
        # _notify_registered_device). The page renders them and then sends "ack". The page
        # also re-registers every few minutes to stay inside API Gateway's idle timeout, which
        # is why this is only sent while something is held.
        try:
            waiting = push_store.get_pending(device_name)
            if waiting:
                _ack(event, connection_id, {"type": "findings", **waiting["payload"]})
                log.info("delivered held findings to %s", device_name)
        except Exception:
            log.exception("could not deliver held findings to connection_id=%s", connection_id)
        return {"statusCode": 200}

    log.warning("unrecognized route: %s", route)
    return {"statusCode": 400, "body": "unrecognized route"}
