"""
The Alexa alert (src/api/alexa_notify.py): tell a person on their Echo that SHIP
found something. Fakes stand in for the users table, Parameter Store and Amazon's
two web endpoints, so nothing real is read or sent.
"""

import pytest

from src.api import alexa_notify as n
from src.api import main as pipeline


class FakeResponse:
    def __init__(self, status=202, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def world(monkeypatch):
    class World:
        users = ["amzn1.ask.account.AAA", "amzn1.ask.account.BBB"]
        params = {n.CLIENT_ID_PARAM: "cid", n.CLIENT_SECRET_PARAM: "secret"}
        posts = []
        event_status = 202
    w = World()
    w.posts = []
    n._token_cache.update(value=None, expires_at=0.0)
    monkeypatch.setattr(n.alexa_user_store, "list_user_ids", lambda: list(w.users))
    monkeypatch.setattr(n, "_parameter", lambda name: w.params[name])

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        w.posts.append({"url": url, "data": data, "json": json, "headers": headers})
        if url == n.TOKEN_URL:
            return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        return FakeResponse(w.event_status)
    monkeypatch.setattr(n.requests, "post", fake_post)
    return w


def events(w):
    return [p for p in w.posts if p["url"] == n.EVENTS_URL]


def test_everyone_who_used_the_skill_is_sent_a_message_alert_from_ship(world):
    assert n.notify_new_finding("a" * 32, 3) == 2
    sent = events(world)
    assert [p["json"]["relevantAudience"]["payload"]["user"] for p in sent] == world.users
    first = sent[0]["json"]
    assert first["event"]["name"] == "AMAZON.MessageAlert.Activated"
    group = first["event"]["payload"]["messageGroup"]
    assert group == {"creator": {"name": "SHIP"}, "count": 3}
    assert first["relevantAudience"]["type"] == "Unicast" and sent[0]["headers"]["Authorization"] == "Bearer tok"


def test_the_token_request_asks_for_the_proactive_events_scope(world):
    n.notify_new_finding("a" * 32, 1)
    token_call = next(p for p in world.posts if p["url"] == n.TOKEN_URL)
    assert token_call["data"]["scope"] == "alexa::proactive_events"
    assert token_call["data"]["grant_type"] == "client_credentials"


def test_the_token_is_reused_not_fetched_for_every_person(world):
    n.notify_new_finding("a" * 32, 1)
    assert sum(1 for p in world.posts if p["url"] == n.TOKEN_URL) == 1


def test_each_person_gets_their_own_reference_id_and_the_event_has_a_valid_window(world):
    n.notify_new_finding("b" * 32, 1)
    refs = [p["json"]["referenceId"] for p in events(world)]
    assert len(set(refs)) == 2 and all(r.isalnum() for r in refs)
    body = events(world)[0]["json"]
    assert body["timestamp"] < body["expiryTime"] and body["localizedAttributes"] == [{"locale": "en-US"}]


def test_a_count_below_one_is_sent_as_one(world):
    n.notify_new_finding("a" * 32, 0)
    assert events(world)[0]["json"]["event"]["payload"]["messageGroup"]["count"] == 1


def test_nobody_to_tell_is_not_an_error(world):
    world.users = []
    assert n.notify_new_finding("a" * 32, 1) == 0 and events(world) == []


def test_missing_credentials_skip_the_alert_without_failing(world):
    world.params = {}
    assert n.notify_new_finding("a" * 32, 1) == 0 and events(world) == []


def test_alexas_own_customer_limit_is_not_treated_as_a_fault(world, caplog):
    world.event_status = n.TOO_MANY_FOR_CUSTOMER
    assert n.notify_new_finding("a" * 32, 1) == 0
    assert "limit" in caplog.text and "refused" not in caplog.text


def test_a_refusal_is_logged_loudly_and_returns_zero(world, caplog):
    world.event_status = 400
    assert n.notify_new_finding("a" * 32, 1) == 0 and "refused" in caplog.text


def test_nothing_the_notifier_hits_can_raise(world, monkeypatch):
    monkeypatch.setattr(n.alexa_user_store, "list_user_ids", lambda: (_ for _ in ()).throw(RuntimeError("table gone")))
    assert n.notify_new_finding("a" * 32, 1) == 0


# --- when the pipeline calls it ---


@pytest.fixture
def pipeline_calls(monkeypatch):
    from src.agents.detector import DetectorOutput
    sent = []
    monkeypatch.setattr(pipeline, "notify_new_finding", lambda alert_id, count: sent.append((alert_id, count)) or 1)
    monkeypatch.setattr(pipeline, "detect", lambda text: DetectorOutput(
        matched=True, taxonomy_id="PIIE-001", risk_score=8.5, plain_english_summary="s", citation="c", remediation_patch="p"))
    monkeypatch.setattr(pipeline, "post_pr_comment", lambda *a, **k: True)
    monkeypatch.setattr(pipeline, "sync_pr_check", lambda *a, **k: True)
    monkeypatch.setattr(pipeline, "comment_for_finding", lambda alert: "c")
    monkeypatch.setattr(pipeline, "mark_reviewed", lambda repo: None)

    class Summary:
        total = 4
    monkeypatch.setattr(pipeline, "refresh_summary", lambda *a, **k: Summary())
    monkeypatch.setattr(pipeline, "fragment_reported", lambda *a: True)
    done = set()      # the delivery steps recorded so far, as alert_store would keep them
    monkeypatch.setattr(pipeline, "claim_step", lambda alert_id, step: (alert_id, step) not in done)
    monkeypatch.setattr(pipeline, "complete_step", lambda alert_id, step: done.add((alert_id, step)))
    monkeypatch.setattr(pipeline, "release_step", lambda alert_id, step: None)
    return sent


def alert(already_notified):
    class A:
        alert_id = "abc"
        status = "frozen"
        commented_at = None
        notified_at = "2026-10-17T00:00:00+00:00" if already_notified else None
    return lambda **kwargs: A()


def test_a_newly_recorded_finding_alerts_with_the_open_count(pipeline_calls, monkeypatch):
    monkeypatch.setattr(pipeline, "put_alert", alert(False))
    pipeline.process_fragment("pandayv/micro-finance", 1, "f.py", "frag", "sha")
    assert pipeline_calls == [("abc", 4)]


def test_a_finding_already_announced_does_not_alert_again(pipeline_calls, monkeypatch):
    monkeypatch.setattr(pipeline, "put_alert", alert(True))
    pipeline.process_fragment("pandayv/micro-finance", 1, "f.py", "frag", "sha")
    assert pipeline_calls == []
