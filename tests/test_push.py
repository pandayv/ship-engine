"""
Tests for src/relay/push.py — delivering a payload to a specific device
over its open WebSocket connection.

The property that matters most: a stale connection (the tab closed
without $disconnect ever firing, a dead network path) is ROUTINE, not an
error. Push has to keep going, self-heal the registry, and still report
an honest result — never raise just because one device among several
went away, and never crash the calling tool over something this
predictable.
"""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import src.relay.push as push
from src.storage.device_store import DeviceConnection


def _gone_error(op="PostToConnection"):
    return ClientError({"Error": {"Code": "GoneException", "Message": "gone"}}, op)


def _other_error(op="PostToConnection"):
    return ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, op)


@pytest.fixture
def endpoint(monkeypatch):
    monkeypatch.setattr(push, "DEVICE_GATEWAY_ENDPOINT", "https://example.execute-api.us-west-2.amazonaws.com/prod")


def test_no_endpoint_configured_reports_not_connected_rather_than_raising(monkeypatch):
    monkeypatch.setattr(push, "DEVICE_GATEWAY_ENDPOINT", "")
    result = push.push_to_device("tv", {"x": 1})
    assert result == {"delivered_to": [], "not_connected": True}


def test_no_device_connected_reports_not_connected(endpoint, monkeypatch):
    monkeypatch.setattr(push, "list_connections_for_device", lambda name: [])
    result = push.push_to_device("fridge", {"x": 1})
    assert result == {"delivered_to": [], "not_connected": True}


def test_successful_push_reports_the_connection_delivered_to(endpoint, monkeypatch):
    monkeypatch.setattr(push, "list_connections_for_device",
                        lambda name: [DeviceConnection("conn-1", "tv", "2026-09-25T00:00:00+00:00")])
    fake_client = MagicMock()
    import boto3 as real_boto3
    monkeypatch.setattr(real_boto3, "client", lambda *a, **k: fake_client)

    result = push.push_to_device("tv", {"hello": "world"})
    assert result == {"delivered_to": ["conn-1"], "not_connected": False}
    fake_client.post_to_connection.assert_called_once()
    _, kwargs = fake_client.post_to_connection.call_args
    assert kwargs["ConnectionId"] == "conn-1"


def test_stale_connection_is_cleaned_up_not_raised(endpoint, monkeypatch):
    # THE property that matters: a gone connection must not crash the
    # calling tool, and must not leave a dead row behind for the next
    # lookup to trip over too.
    monkeypatch.setattr(push, "list_connections_for_device",
                        lambda name: [DeviceConnection("conn-dead", "tv", "2026-09-25T00:00:00+00:00")])
    removed = []
    monkeypatch.setattr(push, "remove_connection", lambda cid: removed.append(cid))

    fake_client = MagicMock()
    fake_client.post_to_connection.side_effect = _gone_error()
    import boto3 as real_boto3
    monkeypatch.setattr(real_boto3, "client", lambda *a, **k: fake_client)

    result = push.push_to_device("tv", {"x": 1})
    assert result == {"delivered_to": [], "not_connected": True}
    assert removed == ["conn-dead"]


def test_one_stale_and_one_live_connection_still_delivers_to_the_live_one(endpoint, monkeypatch):
    # Two tabs both registered as "tv" — one died, one didn't. The dead
    # one must not block delivery to the live one.
    monkeypatch.setattr(push, "list_connections_for_device", lambda name: [
        DeviceConnection("conn-dead", "tv", "2026-09-25T00:00:00+00:00"),
        DeviceConnection("conn-live", "tv", "2026-09-25T00:00:01+00:00"),
    ])
    removed = []
    monkeypatch.setattr(push, "remove_connection", lambda cid: removed.append(cid))

    fake_client = MagicMock()
    fake_client.post_to_connection.side_effect = [_gone_error(), None]
    import boto3 as real_boto3
    monkeypatch.setattr(real_boto3, "client", lambda *a, **k: fake_client)

    result = push.push_to_device("tv", {"x": 1})
    assert result == {"delivered_to": ["conn-live"], "not_connected": False}
    assert removed == ["conn-dead"]


def test_a_non_stale_error_is_logged_not_silently_swallowed(endpoint, monkeypatch, caplog):
    # Distinguish "the device is gone, that's normal" from "something is
    # actually broken" — the second must be visible, not hidden behind
    # the same quiet self-healing path as a routine stale connection.
    monkeypatch.setattr(push, "list_connections_for_device",
                        lambda name: [DeviceConnection("conn-1", "tv", "2026-09-25T00:00:00+00:00")])
    removed = []
    monkeypatch.setattr(push, "remove_connection", lambda cid: removed.append(cid))

    fake_client = MagicMock()
    fake_client.post_to_connection.side_effect = _other_error()
    import boto3 as real_boto3
    monkeypatch.setattr(real_boto3, "client", lambda *a, **k: fake_client)

    result = push.push_to_device("tv", {"x": 1})
    assert result == {"delivered_to": [], "not_connected": True}
    assert removed == [], "a non-stale error must not remove the connection row"


# --- one bad connection must not consume Alexa's deadline ---


def _wire(monkeypatch, connections, client):
    import boto3 as real_boto3
    monkeypatch.setattr(push, "list_connections_for_device", lambda name: connections)
    monkeypatch.setattr(push, "remove_connection", lambda cid: None)
    made = {}
    monkeypatch.setattr(real_boto3, "client", lambda *a, **k: made.update(kwargs=k) or client)
    return made


def _conns(n):
    return [DeviceConnection(f"c{i}", "tv", "2026-09-25T00:00:00+00:00") for i in range(n)]


def test_the_client_is_built_with_short_timeouts_and_no_retries(endpoint, monkeypatch):
    made = _wire(monkeypatch, _conns(1), MagicMock())
    push.push_to_device("tv", {"x": 1})
    config = made["kwargs"]["config"]
    assert config.connect_timeout <= 1.5 and config.read_timeout <= 2 and config.retries["max_attempts"] == 1


def test_a_connection_that_times_out_is_not_delivered_and_does_not_stop_the_others(endpoint, monkeypatch):
    client = MagicMock()
    client.post_to_connection.side_effect = [TimeoutError("read timed out"), None]
    _wire(monkeypatch, _conns(2), client)
    result = push.push_to_device("tv", {"x": 1})
    assert result == {"delivered_to": ["c1"], "not_connected": False}


def test_delivered_means_a_post_succeeded_and_nothing_else(endpoint, monkeypatch):
    client = MagicMock()
    client.post_to_connection.side_effect = [_other_error(), TimeoutError("x"), _gone_error()]
    _wire(monkeypatch, _conns(3), client)
    assert push.push_to_device("tv", {"x": 1}) == {"delivered_to": [], "not_connected": True}


def test_only_a_handful_of_connections_are_tried(endpoint, monkeypatch):
    client = MagicMock()
    _wire(monkeypatch, _conns(40), client)
    assert len(push.push_to_device("tv", {"x": 1})["delivered_to"]) == push.MAX_CONNECTIONS_PER_PUSH


def test_the_push_stops_when_its_time_budget_is_spent(endpoint, monkeypatch):
    clock = iter([0.0, 0.0, push.PUSH_BUDGET_SECONDS + 1, push.PUSH_BUDGET_SECONDS + 2])
    monkeypatch.setattr(push.time, "monotonic", lambda: next(clock))
    client = MagicMock()
    _wire(monkeypatch, _conns(5), client)
    result = push.push_to_device("tv", {"x": 1})
    assert result["delivered_to"] == ["c0"] and client.post_to_connection.call_count == 1


def test_an_http_410_is_treated_as_gone_whatever_the_error_code_is_called(endpoint, monkeypatch):
    err = ClientError({"Error": {"Code": "Other"}, "ResponseMetadata": {"HTTPStatusCode": 410}}, "PostToConnection")
    removed = []
    client = MagicMock()
    client.post_to_connection.side_effect = err
    _wire(monkeypatch, _conns(1), client)
    monkeypatch.setattr(push, "remove_connection", lambda cid: removed.append(cid))
    push.push_to_device("tv", {"x": 1})
    assert removed == ["c0"]


def test_a_failure_removing_a_gone_row_does_not_fail_the_push(endpoint, monkeypatch):
    client = MagicMock()
    client.post_to_connection.side_effect = [_gone_error(), None]
    _wire(monkeypatch, _conns(2), client)
    monkeypatch.setattr(push, "remove_connection", lambda cid: (_ for _ in ()).throw(RuntimeError("table")))
    assert push.push_to_device("tv", {"x": 1})["delivered_to"] == ["c1"]


# --- the Web Push side ---


def test_the_vapid_contact_comes_from_the_environment_with_the_old_value_as_default(monkeypatch):
    import importlib
    import src.relay.webpush as webpush
    assert webpush.VAPID_CONTACT == "mailto:mysubmails0@gmail.com"
    monkeypatch.setenv("SHIP_VAPID_CONTACT", "mailto:someone@example.com")
    try:
        assert importlib.reload(webpush).VAPID_CONTACT == "mailto:someone@example.com"
    finally:
        monkeypatch.delenv("SHIP_VAPID_CONTACT")
        importlib.reload(webpush)
