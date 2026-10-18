"""
The storage rules the gate relies on: what keeps a pull request blocked after a rejection and
what releases it, the review ledger that stops a check going green early, the summary that must
never read an unreadable store as "all clear", and the screen registry that must not list dead
connections.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.storage import alert_store, device_store, review_ledger, status_store
from src.storage.status_store import SummaryUnavailable
from tests.fake_dynamo import ConditionalCheckFailedException, FakeTable


@pytest.fixture
def alerts(monkeypatch):
    fake = FakeTable("alert_id")
    monkeypatch.setattr(alert_store, "_table", lambda: fake)
    return fake


def put(file="a.py", text="frag", pr=3, severity="blocking"):
    return alert_store.put_alert(repo="pandayv/micro-finance", pr_number=pr, file=file, taxonomy_id="PIIE-001",
                                 fragment_text=text, risk_score=8.0, plain_english_summary="s", citation="c",
                                 remediation_patch="p", severity=severity, head_sha="sha")


# --- a rejection holds the PR until the code changes ---


def test_a_rejected_finding_is_decided_so_it_is_not_in_the_review_queue_but_still_blocks(alerts):
    a = put()
    alert_store.resolve_alert(a.alert_id, approved=False, reason="real", resolved_by="x")
    assert alert_store.list_active_alerts() == [] or all(x.status == "frozen" for x in alert_store.list_active_alerts())
    gate = alert_store.list_gate_alerts()
    assert [g.alert_id for g in gate] == [a.alert_id] and alert_store.is_held(gate[0]) and not alert_store.is_open(gate[0])


def test_an_approved_finding_is_neither_open_nor_held(alerts):
    a = put()
    alert_store.resolve_alert(a.alert_id, approved=True, reason="fixture", resolved_by="x")
    assert alert_store.list_gate_alerts() == []


def test_the_gate_list_holds_waiting_findings_and_held_ones_together(alerts):
    waiting, rejected = put(text="one"), put(text="two")
    alert_store.resolve_alert(rejected.alert_id, approved=False, reason="real", resolved_by="x")
    gate = {g.alert_id: g for g in alert_store.list_gate_alerts()}
    assert alert_store.is_open(gate[waiting.alert_id]) and alert_store.is_held(gate[rejected.alert_id])


def test_a_commit_without_the_flagged_code_releases_the_rejected_finding(alerts):
    a = put()
    alert_store.resolve_alert(a.alert_id, approved=False, reason="real", resolved_by="x")
    released = alert_store.release_fixed_rejections("pandayv/micro-finance", 3, present_fragment_keys=set())
    assert released == 1 and alert_store.list_gate_alerts() == []
    assert alerts.rows[a.alert_id]["unblocked_at"]


def test_a_commit_that_still_contains_the_flagged_code_keeps_it_blocked(alerts):
    a = put()
    alert_store.resolve_alert(a.alert_id, approved=False, reason="real", resolved_by="x")
    released = alert_store.release_fixed_rejections("pandayv/micro-finance", 3, {a.fragment_key})
    assert released == 0 and len(alert_store.list_gate_alerts()) == 1


def test_changed_code_gets_a_new_key_and_a_new_finding(alerts):
    a = put(text="old code")
    b = put(text="new code")
    assert a.fragment_key != b.fragment_key and a.alert_id != b.alert_id


def test_releasing_one_pr_never_touches_another(alerts):
    a, other = put(pr=3), put(pr=4)
    for x in (a, other):
        alert_store.resolve_alert(x.alert_id, approved=False, reason="real", resolved_by="x")
    alert_store.release_fixed_rejections("pandayv/micro-finance", 3, set())
    assert [g.alert_id for g in alert_store.list_gate_alerts()] == [other.alert_id]


def test_a_finding_rejected_before_fragment_keys_existed_is_not_held(alerts):
    a = put()
    row = alerts.rows[a.alert_id]
    row.update(status="resolved", resolution="rejected")
    row.pop("fragment_key")
    assert alert_store.list_gate_alerts() == []


def test_an_older_reader_is_not_broken_by_attributes_a_newer_writer_added(alerts):
    a = put()
    alerts.rows[a.alert_id]["something_from_the_future"] = "x"
    assert alert_store.get_alert(a.alert_id).alert_id == a.alert_id


def test_every_scan_that_feeds_a_decision_is_strongly_consistent(alerts):
    put()
    alert_store.list_gate_alerts()
    alert_store.list_active_alerts()
    alert_store.list_alerts_for_pr("pandayv/micro-finance", 3)
    alert_store.get_alert("x")
    assert all(kw.get("ConsistentRead") is True for _, kw in alerts.calls if _ in ("scan", "get_item"))


# --- the review ledger ---


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeTable("summary_id")
    monkeypatch.setattr(status_store, "_table", lambda: fake)
    return fake


def test_the_check_is_complete_only_when_every_expected_fragment_has_reported(ledger):
    assert review_ledger.begin_review("r", 1, "sha1", ["k1", "k2"])
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is False
    assert review_ledger.fragment_reported("r", 1, "sha1", "k2") is True


def test_a_redelivered_message_counts_once(ledger):
    review_ledger.begin_review("r", 1, "sha1", ["k1", "k2"])
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is False
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is False


def test_a_fragment_that_never_reports_keeps_the_check_from_completing(ledger):
    review_ledger.begin_review("r", 1, "sha1", ["k1", "too-large"])
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is False


def test_a_result_for_an_older_commit_does_not_complete_the_newer_review(ledger):
    review_ledger.begin_review("r", 1, "sha2", ["k1"])
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is False


def test_without_a_ledger_a_fragment_reports_as_before(ledger):
    assert review_ledger.fragment_reported("r", 1, "sha1", "k1") is True


def test_a_ledger_read_error_is_raised_so_the_caller_can_hold_the_check(monkeypatch):
    def broken():
        raise RuntimeError("down")
    monkeypatch.setattr(status_store, "_table", broken)
    with pytest.raises(RuntimeError):
        review_ledger.fragment_reported("r", 1, "sha1", "k1")


def test_the_ledger_shares_the_summary_table_without_touching_the_summary(ledger):
    review_ledger.begin_review("r", 1, "sha1", ["k"])
    assert set(ledger.rows) == {"review#r#1"}


# --- the summary ---


def test_an_unreadable_summary_is_not_zero(monkeypatch):
    class Broken:
        def get_item(self, **kw):
            raise RuntimeError("down")
    monkeypatch.setattr(status_store, "_table", lambda: Broken())
    with pytest.raises(SummaryUnavailable):
        status_store.read_summary()


def test_a_summary_that_was_never_written_is_not_zero(ledger):
    with pytest.raises(SummaryUnavailable):
        status_store.read_summary()


def test_a_stored_zero_is_genuinely_zero(ledger, alerts):
    status_store.refresh_summary([])
    assert status_store.read_summary().total == 0


def test_the_summary_counts_held_findings_separately_from_ones_to_decide(ledger, alerts):
    waiting, rejected = put(text="one"), put(text="two")
    alert_store.resolve_alert(rejected.alert_id, approved=False, reason="real", resolved_by="x")
    summary = status_store.refresh_summary(alert_store.list_gate_alerts())
    assert (summary.blocking, summary.held) == (1, 1) and summary.pull_requests[0].held == 1
    assert status_store.read_summary().held == 1


def test_an_older_computation_cannot_overwrite_a_newer_summary(ledger, alerts, monkeypatch):
    put(text="one")
    newer = status_store.refresh_summary([])
    assert status_store.read_summary().computed_at == newer.computed_at
    # A slow writer that started earlier finishes now, having read one finding.
    older_alerts = alert_store.list_gate_alerts()
    monkeypatch.setattr(status_store, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime.fromisoformat(newer.computed_at) - timedelta(seconds=5))}))
    status_store.refresh_summary(older_alerts)
    stored = status_store.read_summary()
    assert stored.computed_at == newer.computed_at and stored.total == 0


def test_a_refresh_failure_never_raises(monkeypatch):
    monkeypatch.setattr(status_store, "_table", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert status_store.refresh_summary([]).total == 0


# --- screens ---


def conn(name, connected_ago, seen_ago=None):
    now = datetime.now(timezone.utc)
    return device_store.DeviceConnection(
        "c", name, (now - timedelta(seconds=connected_ago)).isoformat(),
        (now - timedelta(seconds=seen_ago)).isoformat() if seen_ago is not None else "")


def test_a_connection_past_the_api_gateway_hard_cap_is_dead():
    assert device_store.is_live(conn("tv", connected_ago=60, seen_ago=60))
    assert not device_store.is_live(conn("tv", connected_ago=2 * 3600 + 5, seen_ago=60))


def test_a_connection_silent_past_the_idle_limit_is_dead():
    assert not device_store.is_live(conn("tv", connected_ago=1800, seen_ago=device_store.CONNECTION_IDLE_SECONDS + 5))


def test_a_row_without_last_seen_falls_back_to_when_it_connected():
    assert device_store.is_live(conn("tv", connected_ago=120))
    assert not device_store.is_live(conn("tv", connected_ago=3600))


def test_an_unreadable_timestamp_is_kept_rather_than_dropped():
    assert device_store.is_live(device_store.DeviceConnection("c", "tv", "garbage"))


def test_registering_again_keeps_when_it_first_connected_and_refreshes_last_seen(monkeypatch):
    fake = FakeTable("connection_id")
    monkeypatch.setattr(device_store, "_table", lambda: fake)
    device_store.register_connection("c1", "Tablet")
    first = dict(fake.rows["c1"])
    time.sleep(0.01)
    device_store.register_connection("c1", "Tablet")
    row = fake.rows["c1"]
    assert row["connected_at"] == first["connected_at"] and row["last_seen_at"] > first["last_seen_at"]
    assert row["device_name"] == "tablet" and row["expires_at"] > time.time()


def test_dead_rows_are_not_listed_and_new_attributes_do_not_break_the_reader(monkeypatch):
    fake = FakeTable("connection_id")
    monkeypatch.setattr(device_store, "_table", lambda: fake)
    now = datetime.now(timezone.utc)
    fake.rows = {
        "alive": {"connection_id": "alive", "device_name": "tv", "connected_at": now.isoformat(),
                  "last_seen_at": now.isoformat(), "expires_at": 5, "a_future_field": 1},
        "dead": {"connection_id": "dead", "device_name": "tv",
                 "connected_at": (now - timedelta(hours=3)).isoformat()},
    }
    assert [c.connection_id for c in device_store.list_all_connections()] == ["alive"]
    assert [c.connection_id for c in device_store.list_connections_for_device("tv")] == ["alive"]
