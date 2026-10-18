"""
Reaching a screen whose page is closed: registration, the notification, the findings
held for it, and the rule that a notification does not count as having seen anything.

Fakes stand in for DynamoDB, the browser push service and the WebSocket API.
"""

import json
import time

import pytest

import device_gateway_handler as gateway
from src.relay import server as relay
from src.relay import webpush
from src.storage import push_store


class FakeTable:
    def __init__(self):
        self.rows = {}

    def put_item(self, Item, **kw):
        self.rows[next(iter(v for k, v in Item.items() if k in ("endpoint_hash", "device_name") and k == self.key))] = dict(Item)

    key = None


@pytest.fixture
def store(monkeypatch):
    subs, pend = FakeTable(), FakeTable()
    subs.key, pend.key = "endpoint_hash", "device_name"
    subs.scan = lambda **kw: {"Items": list(subs.rows.values())}
    subs.delete_item = lambda Key: subs.rows.pop(Key["endpoint_hash"], None)
    pend.get_item = lambda Key: {"Item": pend.rows[Key["device_name"]]} if Key["device_name"] in pend.rows else {}
    pend.delete_item = lambda Key: pend.rows.pop(Key["device_name"], None)
    monkeypatch.setattr(push_store, "_subscriptions", lambda: subs)
    monkeypatch.setattr(push_store, "_pending", lambda: pend)
    return subs, pend


SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


# --- the store ---

def test_a_subscription_is_saved_under_a_lowercase_screen_name(store):
    push_store.save_subscription("Tablet", SUB)
    assert [r["subscription"] for r in push_store.subscriptions_for_device("tablet")] == [SUB]
    assert push_store.subscribed_device_names() == {"tablet"}


def test_the_same_browser_registering_again_replaces_its_row(store):
    push_store.save_subscription("tablet", SUB)
    push_store.save_subscription("desk", SUB)
    assert push_store.subscribed_device_names() == {"desk"}


def test_something_that_is_not_a_subscription_is_refused(store):
    with pytest.raises(ValueError):
        push_store.save_subscription("tablet", {"endpoint": "x"})


def test_held_findings_expire(store, monkeypatch):
    push_store.save_pending("tablet", {"pull_requests": []}, ["a1"])
    assert push_store.get_pending("tablet")["alert_ids"] == ["a1"]
    real_now = time.time()
    monkeypatch.setattr(push_store.time, "time", lambda: real_now + push_store.PENDING_TTL_SECONDS + 5)
    assert push_store.get_pending("tablet") is None


# --- sending ---

def test_a_notification_goes_to_every_browser_under_the_name_and_reports_how_many(store, monkeypatch):
    push_store.save_subscription("tablet", SUB)
    sent = []
    monkeypatch.setattr(webpush, "_send", lambda sub, msg: sent.append((sub, msg)) or type("R", (), {"status_code": 201})())
    assert webpush.notify_device("tablet", "2 findings need a decision. Tap to review.") == 1
    assert sent[0][1]["body"].startswith("2 findings") and sent[0][1]["url"].endswith("ship-display.html")


def test_a_subscription_the_browser_says_is_gone_is_removed(store, monkeypatch):
    push_store.save_subscription("tablet", SUB)
    monkeypatch.setattr(webpush, "_send", lambda sub, msg: type("R", (), {"status_code": 410})())
    assert webpush.notify_device("tablet", "x") == 0
    assert push_store.subscribed_device_names() == set()


def test_a_failure_to_send_never_raises(store, monkeypatch):
    push_store.save_subscription("tablet", SUB)
    monkeypatch.setattr(webpush, "_send", lambda sub, msg: (_ for _ in ()).throw(RuntimeError("network")))
    assert webpush.notify_device("tablet", "x") == 0


# --- Relay: show it on a screen whose page is closed ---

class Finding:
    def __init__(self, alert_id):
        self.alert_id, self.severity = alert_id, relay.SEVERITY_BLOCKING


class PR:
    repo, pr_number = "pandayv/micro-finance", 1

    def __init__(self, *ids):
        self.findings = [Finding(i) for i in ids]
        self.blocking, self.review = len(ids), 0


class Status:
    def __init__(self, *ids):
        self.pull_requests = [PR(*ids)]
        self.blocking, self.review, self.total = len(ids), 0, len(ids)


@pytest.fixture
def relay_world(monkeypatch, store):
    shown, notified = [], []
    monkeypatch.setattr(relay, "_findings_detail", lambda: Status("a1", "a2"))
    monkeypatch.setattr(relay, "_findings_payload", lambda status: {"pull_requests": [], "display_only": True})
    monkeypatch.setattr(relay.session_store, "mark_shown", lambda alert_id: shown.append(alert_id))
    monkeypatch.setattr(relay.session_store, "was_shown_recently", lambda alert_id: alert_id in shown)
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": [], "not_connected": True})
    monkeypatch.setattr(relay.webpush, "notify_device", lambda name, body: notified.append((name, body)) or 1)
    return shown, notified


def test_a_closed_page_is_sent_a_notification_and_the_findings_are_held(relay_world, store):
    shown, notified = relay_world
    push_store.save_subscription("tablet", SUB)
    result = relay.display_on("tablet")
    assert result["notified"] is True and result["delivered"] is False
    assert "sent a notification" in result["spoken_response"].lower()
    assert notified[0][0] == "tablet" and "2 findings need a decision" in notified[0][1]
    assert push_store.get_pending("tablet")["alert_ids"] == ["a1", "a2"]


def test_a_notification_does_not_count_as_having_seen_the_findings(relay_world, store):
    shown, _ = relay_world
    push_store.save_subscription("tablet", SUB)
    relay.display_on("tablet")
    assert shown == []                      # nothing recorded as shown yet


def test_a_screen_nobody_registered_is_still_reported_as_not_there(relay_world, store):
    result = relay.display_on("tablet")
    assert result["notified"] is False and "don't see" in result["spoken_response"]


def test_a_notification_that_could_not_be_sent_is_not_claimed(relay_world, store, monkeypatch):
    push_store.save_subscription("tablet", SUB)
    monkeypatch.setattr(relay.webpush, "notify_device", lambda name, body: 0)
    assert relay.display_on("tablet")["notified"] is False


def test_registered_screens_are_offered_even_with_no_page_open(relay_world, store, monkeypatch):
    push_store.save_subscription("tablet", SUB)
    monkeypatch.setattr(relay, "list_all_connections", lambda: [type("C", (), {"device_name": "laptop"})()])
    result = relay.connected_screens()
    assert result["screens"] == ["laptop", "tablet"] and result["notify_only"] == ["tablet"]


def test_findings_routing_reports_what_was_seen_without_recording_anything(relay_world):
    shown, _ = relay_world
    shown.append("a1")
    findings = relay.findings_routing()["pull_requests"][0]["findings"]
    assert [(f["position"], f["alert_id"], f["seen"]) for f in findings] == [(1, "a1", True), (2, "a2", False)]
    assert shown == ["a1"]                  # reading it marked nothing


# --- the gateway ---

def ws_event(route, body=None):
    return {"requestContext": {"routeKey": route, "connectionId": "c1", "domainName": "x", "stage": "prod"},
            "body": json.dumps(body or {})}


@pytest.fixture
def gw(monkeypatch, store):
    sent, shown = [], []
    monkeypatch.setattr(gateway, "_ack", lambda event, cid, body: sent.append(body))
    monkeypatch.setattr(gateway, "register_connection", lambda cid, name: None)
    monkeypatch.setattr(gateway.session_store, "mark_shown", lambda alert_id: shown.append(alert_id))
    return sent, shown


def test_the_gateway_stores_a_subscription_and_confirms(gw, store):
    sent, _ = gw
    out = gateway.handler(ws_event("subscribe", {"device_name": "Tablet", "subscription": SUB}), None)
    assert out["statusCode"] == 200 and sent == [{"subscribed": "tablet"}]
    assert push_store.subscribed_device_names() == {"tablet"}


def test_a_bad_subscription_is_refused(gw):
    assert gateway.handler(ws_event("subscribe", {"device_name": "t", "subscription": {}}), None)["statusCode"] == 400


def test_a_page_that_connects_receives_the_findings_held_for_it(gw, store):
    sent, _ = gw
    push_store.save_pending("tablet", {"pull_requests": [{"pr_number": 1}]}, ["a1"])
    gateway.handler(ws_event("register", {"device_name": "tablet"}), None)
    assert sent == [{"registered": "tablet"}, {"pull_requests": [{"pr_number": 1}]}]


def test_a_page_with_nothing_held_gets_only_its_confirmation(gw, store):
    sent, _ = gw
    gateway.handler(ws_event("register", {"device_name": "tablet"}), None)
    assert sent == [{"registered": "tablet"}]


def test_the_ack_marks_only_the_findings_that_were_being_held(gw, store):
    _, shown = gw
    push_store.save_pending("tablet", {"pull_requests": []}, ["a1", "a2"])
    gateway.handler(ws_event("ack", {"device_name": "tablet", "alert_ids": ["a1", "other-alert"]}), None)
    assert shown == ["a1"] and push_store.get_pending("tablet") is None


def test_an_ack_with_nothing_held_marks_nothing(gw, store):
    _, shown = gw
    gateway.handler(ws_event("ack", {"device_name": "tablet", "alert_ids": ["a1"]}), None)
    assert shown == []
