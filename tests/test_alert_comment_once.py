"""
A finding is posted to the pull request once, not on every push.

Found by the 2026-10-08 smoke test: a second commit pushed to a PR re-detected
a finding a person had already accepted, and SHIP commented "merge blocked"
again on a PR whose check was green. put_alert() now reports whether it created
the row, and process_fragment() comments only in that case.
"""

import pytest

from src.api.main import process_fragment
from src.agents.detector import DetectorOutput
from src.storage import alert_store


class ConditionalCheckFailedException(Exception):
    """Named exactly like botocore's runtime class: alert_store recognizes DynamoDB's
    refusal by this class name."""


class _FakeTable:
    """Stands in for the DynamoDB table. `existing` is the row already stored."""

    def __init__(self, existing=None, refuse=False):
        self.existing, self.refuse, self.written = existing, refuse, []

    def put_item(self, **kwargs):
        if self.refuse:
            raise ConditionalCheckFailedException()
        self.written.append(kwargs["Item"])
        assert kwargs.get("ReturnValues") == "ALL_OLD"   # the atomic "was it there?" question
        return {"Attributes": self.existing} if self.existing else {}


def _put(table, monkeypatch):
    monkeypatch.setattr(alert_store, "_table", lambda: table)
    return alert_store.put_alert(
        repo="pandayv/micro-finance", pr_number=4, file="loans/a.py", taxonomy_id="PIIE-001",
        fragment_text="prompt = f'{c.email}'", risk_score=8.5, plain_english_summary="s",
        citation="GDPR Article 32", remediation_patch="p",
    )


def test_a_first_detection_is_reported_as_new(monkeypatch):
    assert _put(_FakeTable(), monkeypatch).newly_written is True


def test_re_detecting_a_finding_that_is_still_open_is_not_new(monkeypatch):
    assert _put(_FakeTable(existing={"alert_id": "x", "status": "frozen"}), monkeypatch).newly_written is False


def test_re_detecting_a_finding_a_person_already_resolved_is_not_new(monkeypatch):
    monkeypatch.setattr(alert_store, "_table", lambda: _FakeTable(refuse=True))
    alert = alert_store.put_alert(
        repo="pandayv/micro-finance", pr_number=4, file="loans/a.py", taxonomy_id="PIIE-001",
        fragment_text="x", risk_score=8.5, plain_english_summary="s", citation="c", remediation_patch="p",
    )
    assert alert.newly_written is False


def test_the_marker_is_never_stored_with_the_finding(monkeypatch):
    table = _FakeTable()
    _put(table, monkeypatch)
    assert "newly_written" not in table.written[0]


@pytest.fixture
def pipeline(monkeypatch):
    posted, checks = [], []
    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(
        matched=True, taxonomy_id="PIIE-001", risk_score=8.5,
        plain_english_summary="s", citation="c", remediation_patch="p"))
    monkeypatch.setattr("src.api.main.post_pr_comment", lambda *a, **k: posted.append(a) or True)
    monkeypatch.setattr("src.api.main.sync_pr_check", lambda *a, **k: checks.append(a) or True)
    monkeypatch.setattr("src.api.main.comment_for_finding", lambda alert: "comment")
    monkeypatch.setattr("src.api.main.refresh_summary", lambda: None)
    monkeypatch.setattr("src.api.main.mark_reviewed", lambda repo: None)
    return posted, checks


def _stored(new):
    class A:
        alert_id = "abc"
        newly_written = new
    return lambda **kwargs: A()


def test_a_new_finding_is_commented_on_the_pull_request(pipeline, monkeypatch):
    posted, _ = pipeline
    monkeypatch.setattr("src.api.main.put_alert", _stored(True))
    process_fragment("pandayv/micro-finance", 4, "loans/a.py", "frag", "sha1")
    assert len(posted) == 1


def test_a_finding_already_on_record_is_not_commented_again(pipeline, monkeypatch):
    posted, _ = pipeline
    monkeypatch.setattr("src.api.main.put_alert", _stored(False))
    process_fragment("pandayv/micro-finance", 4, "loans/a.py", "frag", "sha2")
    assert posted == []


def test_the_check_is_recomputed_even_when_no_comment_is_posted(pipeline, monkeypatch):
    _, checks = pipeline
    monkeypatch.setattr("src.api.main.put_alert", _stored(False))
    process_fragment("pandayv/micro-finance", 4, "loans/a.py", "frag", "sha2")
    assert len(checks) == 1
