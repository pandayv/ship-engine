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
import time

from src.storage.device_store import list_connections_for_device, remove_connection

log = logging.getLogger(__name__)

# The Management API needs a REST-style HTTPS endpoint for this specific
# WebSocket API + stage, a different address from the wss:// URL a
# browser connects to — set post-deploy, once the API exists, same
# pattern as every other "doesn't exist until its resource is created"
# value in this project (SHIP_AGENTCORE_RUNTIME_ARN, SHIP_RELAY_ALLOWED_HOSTS).
DEVICE_GATEWAY_ENDPOINT = os.environ.get("SHIP_DEVICE_GATEWAY_ENDPOINT", "")

# Alexa gives the skill 8 seconds and the skill gives Relay 5. One unreachable
# connection must not eat that, so every post has a short timeout, only a handful of
# connections per name are tried, and the whole push stops when its budget is spent.
POST_CONNECT_TIMEOUT = 1.0
POST_READ_TIMEOUT = 1.5
MAX_CONNECTIONS_PER_PUSH = 6
PUSH_BUDGET_SECONDS = 2.0


def _is_gone(error) -> bool:
    response = getattr(error, "response", None) or {}
    return (response.get("Error", {}).get("Code") == "GoneException"
            or response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 410)


def push_to_device(device_name: str, payload: dict) -> dict:
    """Returns {"delivered_to": [...], "not_connected": bool} — always a
    result, never an exception for the ordinary case of a device that
    isn't currently connected. That is expected, steady-state behavior
    for an ambient display that might not be powered on. A connection counts as
    delivered only after its post succeeded."""
    if not DEVICE_GATEWAY_ENDPOINT:
        log.warning("SHIP_DEVICE_GATEWAY_ENDPOINT not configured — cannot push to %r", device_name)
        return {"delivered_to": [], "not_connected": True}

    connections = list_connections_for_device(device_name)
    if not connections:
        return {"delivered_to": [], "not_connected": True}

    import json

    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "apigatewaymanagementapi", endpoint_url=DEVICE_GATEWAY_ENDPOINT,
        config=Config(connect_timeout=POST_CONNECT_TIMEOUT, read_timeout=POST_READ_TIMEOUT, retries={"max_attempts": 1}),
    )
    body = json.dumps(payload).encode("utf-8")

    delivered = []
    started = time.monotonic()
    for conn in connections[:MAX_CONNECTIONS_PER_PUSH]:
        if time.monotonic() - started > PUSH_BUDGET_SECONDS:
            log.warning("push to %r stopped at its time budget after %d of %d connections",
                        device_name, len(delivered), len(connections))
            break
        try:
            client.post_to_connection(ConnectionId=conn.connection_id, Data=body)
            delivered.append(conn.connection_id)
        except ClientError as e:
            if _is_gone(e):
                # Routine — see module docstring. Self-heal the registry
                # rather than leaving a dead row for the next lookup to
                # trip over too.
                try:
                    remove_connection(conn.connection_id)
                except Exception:
                    log.warning("could not remove the gone connection %s", conn.connection_id, exc_info=True)
            else:
                log.exception("push to %s failed for a non-stale reason", conn.connection_id)
        except Exception:
            # A timeout or a network error: this connection did not receive it.
            log.warning("push to %s did not complete", conn.connection_id, exc_info=True)

    return {"delivered_to": delivered, "not_connected": len(delivered) == 0}
