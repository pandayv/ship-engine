"""
Tells a person, on their Echo, that SHIP found something.

When SHIP records a new finding it sends an Alexa notification to everyone who
has used the SHIP Alexa Skill. Their Echo chimes and shows an indicator; they
then ask "what's up" and the skill answers. Nothing is read out unprompted.

Alexa has no generic alert type for skills, so this uses its message alert:
"You have 2 new unread messages from SHIP", where the count is the number of
open findings.

Like the pull request comment and the commit status, this can never break a
review. The finding is already stored by the time it runs, so every failure is
logged and swallowed. It sends only when a finding is newly recorded, so a later
push to the same pull request does not chime again.

The skill's Login with Amazon client id and secret live in SSM Parameter Store
as SecureString parameters, not in the function's environment.
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from src.storage import alexa_user_store

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.amazon.com/auth/o2/token"
EVENTS_URL = os.environ.get("SHIP_ALEXA_EVENTS_URL", "https://api.amazonalexa.com/v1/proactiveEvents/stages/development")
CLIENT_ID_PARAM = "/ship/alexa/client_id"
CLIENT_SECRET_PARAM = "/ship/alexa/client_secret"
REQUEST_TIMEOUT = 6
TOO_MANY_FOR_CUSTOMER = 432     # Alexa's own daily limit per customer; an expected answer, not a fault

_token_cache = {"value": None, "expires_at": 0.0}


def _parameter(name: str) -> str:
    import boto3

    return boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _access_token() -> str | None:
    """A Login with Amazon token for the skill, reused until shortly before it expires."""
    if _token_cache["value"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["value"]
    try:
        client_id, client_secret = _parameter(CLIENT_ID_PARAM), _parameter(CLIENT_SECRET_PARAM)
    except Exception:  # noqa: BLE001
        log.error("Alexa alert skipped: the skill credentials are not in Parameter Store (%s, %s)",
                  CLIENT_ID_PARAM, CLIENT_SECRET_PARAM)
        return None
    response = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": client_id,
              "client_secret": client_secret, "scope": "alexa::proactive_events"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    _token_cache["value"] = body["access_token"]
    _token_cache["expires_at"] = time.time() + int(body.get("expires_in", 3600))
    return _token_cache["value"]


def _event(user_id: str, alert_id: str, open_findings: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.00Z"),
        # Unique per finding and per person, so a retried job is recognisable as the same event.
        "referenceId": f"{alert_id[:24]}{hashlib.sha1(user_id.encode()).hexdigest()[:8]}",
        "expiryTime": (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.00Z"),
        "event": {
            "name": "AMAZON.MessageAlert.Activated",
            "payload": {
                "state": {"status": "UNREAD", "freshness": "NEW"},
                "messageGroup": {"creator": {"name": "SHIP"}, "count": max(1, int(open_findings))},
            },
        },
        "localizedAttributes": [{"locale": "en-US"}],
        "relevantAudience": {"type": "Unicast", "payload": {"user": user_id}},
    }


def notify_new_finding(alert_id: str, open_findings: int) -> int:
    """Sends the notification to every saved Alexa user. Returns how many were accepted.
    Never raises."""
    try:
        users = alexa_user_store.list_user_ids()
        if not users:
            log.info("Alexa alert skipped: nobody has opened the SHIP skill yet")
            return 0
        token = _access_token()
        if not token:
            return 0
        accepted = 0
        for user_id in users:
            response = requests.post(
                EVENTS_URL, json=_event(user_id, alert_id, open_findings),
                headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 202:
                accepted += 1
            elif response.status_code == TOO_MANY_FOR_CUSTOMER:
                log.info("Alexa alert not sent: that customer has reached Alexa's notification limit")
            else:
                log.error("Alexa alert refused: status=%s body=%s", response.status_code, response.text[:200])
        log.info("Alexa alert sent to %d of %d people", accepted, len(users))
        return accepted
    except Exception:  # noqa: BLE001 - an alert must never fail a review
        log.exception("Alexa alert failed")
        return 0
