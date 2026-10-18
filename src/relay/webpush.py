"""
Sends a push notification to a screen that has no page open.

The browser, not SHIP, delivers it: each browser gives the page a push address,
and a message sent there wakes the page's service worker (docs/ship-sw.js), which
shows the notification. The message is signed with SHIP's own VAPID key so the
browser knows who sent it. The private key lives in Parameter Store as a
SecureString; the public half is in the display page.

A notification carries only a short line ("2 findings need a decision. Tap to
review."), never a finding. Tapping it opens the display page, which then receives
the findings and reports that it displayed them.
"""

import json
import logging
from functools import lru_cache

from src.storage import push_store

log = logging.getLogger(__name__)

VAPID_PRIVATE_PARAM = "/ship/webpush/vapid_private"
VAPID_CONTACT = "mailto:mysubmails0@gmail.com"
DISPLAY_PAGE = "https://pandayv.github.io/ship-engine/ship-display.html"
GONE = (404, 410)       # the browser says this subscription no longer exists


@lru_cache(maxsize=1)
def _vapid_private_key() -> str:
    import boto3

    return boto3.client("ssm").get_parameter(Name=VAPID_PRIVATE_PARAM, WithDecryption=True)["Parameter"]["Value"]


def _send(subscription: dict, message: dict):
    from pywebpush import WebPushException, webpush

    try:
        return webpush(
            subscription_info=subscription,
            data=json.dumps(message),
            vapid_private_key=_vapid_private_key(),
            vapid_claims={"sub": VAPID_CONTACT},
            ttl=600,
            timeout=5,
        )
    except WebPushException as exc:
        return exc.response


def notify_device(device_name: str, body: str) -> int:
    """Sends the notification to every browser registered under this screen name. Returns how
    many accepted it. Never raises: a failed notification is a normal, reportable outcome."""
    message = {"title": "SHIP", "body": body, "url": DISPLAY_PAGE}
    sent = 0
    for row in push_store.subscriptions_for_device(device_name):
        try:
            response = _send(row["subscription"], message)
        except Exception:  # noqa: BLE001
            log.exception("push to %s failed", device_name)
            continue
        status = getattr(response, "status_code", None)
        if status in GONE:
            log.info("push subscription for %s is gone, removing it", device_name)
            push_store.remove_subscription(row["endpoint_hash"])
        elif status is not None and 200 <= status < 300:
            sent += 1
        else:
            log.error("push to %s refused: status=%s", device_name, status)
    return sent
