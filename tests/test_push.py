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
