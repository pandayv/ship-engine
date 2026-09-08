"""
Tests for src/api/github_writeback.py — the leg that puts findings and
human decisions back on the pull request.

The property worth protecting here is race safety. Fragments are processed
in parallel by independently-scaling Lambdas, so several of them can call
sync_pr_check() for the same PR at once. None of them may state the status
from its own local view; each must derive it from stored alerts, so that
concurrent writers converge instead of overwriting each other.
"""

import pytest

import src.api.github_writeback as wb
from src.storage.alert_store import SEVERITY_BLOCKING, SEVERITY_REVIEW, Alert


def _alert(alert_id="a1", severity=SEVERITY_BLOCKING, status="frozen"):
    return Alert(
        alert_id=alert_id, repo="pandayv/micro-finance", pr_number=7,
        file="loans/ai_underwriting.py", taxonomy_id="PIIE-001", risk_score=9.0,
        plain_english_summary="Raw applicant PII reaches an external model. Nothing masks it first.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash the identifiers first",
        status=status, created_at="2026-09-07T00:00:00+00:00",
        severity=severity, head_sha="abc123",
    )


@pytest.fixture
def posted(monkeypatch):
    """Captures what would have been POSTed, so no test touches the network."""
    sent = []

    class _Response:
        def raise_for_status(self): return None

    def _fake_post(url, json=None, headers=None, timeout=None):
        sent.append({"url": url, "body": json})
        return _Response()

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(wb.requests, "post", _fake_post)
    return sent


def _stub_alerts(monkeypatch, alerts):
    monkeypatch.setattr(wb, "list_alerts_for_pr", lambda repo, pr: alerts)


def test_blocking_finding_fails_the_commit_status(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])
    assert wb.sync_pr_check("pandayv/micro-finance", 7, "abc123") is True

    assert posted[0]["url"].endswith("/repos/pandayv/micro-finance/statuses/abc123")
    assert posted[0]["body"]["state"] == "failure"
    assert posted[0]["body"]["context"] == "ship/compliance"


def test_flagged_only_finding_stays_green(monkeypatch, posted):
    # A flagged finding never stopped the merge, so reporting failure would
    # misrepresent what Triage actually decided.
    _stub_alerts(monkeypatch, [_alert(severity=SEVERITY_REVIEW)])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")

    assert posted[0]["body"]["state"] == "success"
    assert "flagged for review" in posted[0]["body"]["description"]


def test_resolved_blocking_finding_turns_the_status_green(monkeypatch, posted):
    # This is the human decision closing the loop: the same function that
    # turned the check red is what turns it green again.
    _stub_alerts(monkeypatch, [_alert(status="resolved")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")

    assert posted[0]["body"]["state"] == "success"
    assert posted[0]["body"]["description"] == "No unresolved compliance findings"


def test_status_is_derived_from_the_store_not_from_the_caller(monkeypatch, posted):
    # The race-safety property. Two fragments finish in either order; the
    # one carrying a clean verdict must not be able to clear a blocking
    # finding another fragment already stored, because neither states the
    # status — both recompute it from the same shared data.
    _stub_alerts(monkeypatch, [
        _alert("blocking-one", severity=SEVERITY_BLOCKING),
        _alert("flagged-two", severity=SEVERITY_REVIEW),
    ])

    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")  # as if from fragment A
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")  # as if from fragment B

    assert [p["body"]["state"] for p in posted] == ["failure", "failure"]


def test_multiple_blocking_findings_are_counted_in_the_description(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert("one"), _alert("two")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["description"].startswith("2 findings")


def test_local_test_repo_never_writes_back(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])
    assert wb.sync_pr_check("local-test", 0, "abc123") is False
    assert posted == []


def test_missing_head_sha_is_skipped_rather_than_guessed(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])
    assert wb.sync_pr_check("pandayv/micro-finance", 7, "") is False
    assert posted == []


def test_no_token_configured_skips_instead_of_failing(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert wb.post_pr_comment("pandayv/micro-finance", 7, "body") is False


def test_github_failure_is_soft_and_does_not_raise(monkeypatch):
    # A compliance gate that crashes the whole review because GitHub had a
    # blip is worse than one that logs loudly and carries on — the alert is
    # already durably stored by the time this runs, and raising would send
    # the SQS message back for a retry that re-runs the model call.
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def _boom(*args, **kwargs):
        raise wb.requests.exceptions.ConnectionError("github unreachable")

    monkeypatch.setattr(wb.requests, "post", _boom)
    assert wb.post_pr_comment("pandayv/micro-finance", 7, "body") is False


def test_finding_comment_carries_the_citation_and_the_patch():
    body = wb.comment_for_finding(_alert())
    assert "GDPR Art. 32(1)(a)" in body
    assert "hash the identifiers first" in body
    assert "merge blocked" in body
    assert "loans/ai_underwriting.py" in body


def test_flagged_finding_comment_does_not_claim_the_merge_is_blocked():
    body = wb.comment_for_finding(_alert(severity=SEVERITY_REVIEW))
    assert "does not stop the merge" in body
    assert "merge blocked" not in body


def test_finding_comment_does_not_repeat_the_summary_in_its_heading():
    alert = _alert()
    body = wb.comment_for_finding(alert)
    first_sentence = alert.plain_english_summary.split(".")[0]
    assert body.count(first_sentence) == 1


def test_decision_comment_records_who_decided_and_why():
    body = wb.comment_for_decision(
        _alert(), accepted=True, reason="Fixture data only, tracked as LOAN-812.", who="Vipul")
    assert "Risk accepted" in body
    assert "Vipul" in body
    assert "Fixture data only, tracked as LOAN-812." in body
    assert "set to passing" in body


def test_confirming_a_finding_says_the_check_stays_failing():
    body = wb.comment_for_decision(
        _alert(), accepted=False, reason="Real problem, allowlist landing this sprint.", who="Vipul")
    assert "needs a fix" in body
    assert "stays failing" in body
