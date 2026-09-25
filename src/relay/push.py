"""
Pushes a payload to a specific, named device over its open WebSocket
connection. The write side of the push architecture — see
src/storage/device_store.py for the connection registry this reads.

STALE CONNECTIONS ARE ROUTINE, NOT ERRORS. A WebSocket connection can
disappear without $disconnect ever firing — a closed laptop lid, a
network blip, a browser tab killed outright. API Gateway's Management
API surfaces this as GoneException on the next post_to_connection call
against that connection_id. This is an entirely expected steady-state
condition for a store of "devices that were connected as of their last
successful registration," not a failure — so it is caught here and the
stale row is cleaned up (self-healing the registry) rather than raised.
A device with genuinely no live connection is reported back as
delivered=False so the caller (a Relay tool) can say so honestly,
without the request itself failing.
"""

import logging
import os

from src.storage.device_store import list_connections_for_device, remove_connection

log = logging.getLogger(__name__)

# The Management API needs a REST-style HTTPS endpoint for this specific
# WebSocket API + stage, a different address from the wss:// URL a
# browser connects to — set post-deploy, once the API exists, same
# pattern as every other "doesn't exist until its resource is created"
# value in this project (SHIP_AGENTCORE_RUNTIME_ARN, SHIP_RELAY_ALLOWED_HOSTS).
DEVICE_GATEWAY_ENDPOINT = os.environ.get("SHIP_DEVICE_GATEWAY_ENDPOINT", "")


def push_to_device(device_name: str, payload: dict) -> dict:
    """Returns {"delivered_to": [...], "not_connected": bool} — always a
    result, never an exception for the ordinary case of a device that
    isn't currently connected. That is expected, steady-state behavior
    for an ambient display that might not be powered on."""
    if not DEVICE_GATEWAY_ENDPOINT:
        log.warning("SHIP_DEVICE_GATEWAY_ENDPOINT not configured — cannot push to %r", device_name)
        return {"delivered_to": [], "not_connected": True}

    connections = list_connections_for_device(device_name)
    if not connections:
        return {"delivered_to": [], "not_connected": True}

    import json

    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("apigatewaymanagementapi", endpoint_url=DEVICE_GATEWAY_ENDPOINT)
    body = json.dumps(payload).encode("utf-8")

    delivered = []
    for conn in connections:
        try:
            client.post_to_connection(ConnectionId=conn.connection_id, Data=body)
            delivered.append(conn.connection_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "GoneException":
                # Routine — see module docstring. Self-heal the registry
                # rather than leaving a dead row for the next lookup to
                # trip over too.
                remove_connection(conn.connection_id)
            else:
                log.exception("push to %s failed for a non-stale reason", conn.connection_id)

    return {"delivered_to": delivered, "not_connected": len(delivered) == 0}
