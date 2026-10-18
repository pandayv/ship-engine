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


def _alert(alert_id="a1", severity=SEVERITY_BLOCKING, status="frozen", resolution=None, fragment_key="", unblocked_at=None):
    return Alert(
        alert_id=alert_id, repo="pandayv/micro-finance", pr_number=7,
        file="loans/ai_underwriting.py", taxonomy_id="PIIE-001", risk_score=9.0,
        plain_english_summary="Raw applicant PII reaches an external model. Nothing masks it first.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash the identifiers first",
        status=status, created_at="2026-09-07T00:00:00+00:00",
        severity=severity, head_sha="abc123",
        resolution=resolution, fragment_key=fragment_key, unblocked_at=unblocked_at,
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

    class _Head:
        def raise_for_status(self): return None
        def json(self): return {"head": {"sha": "abc123"}}

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(wb.requests, "post", _fake_post)
    monkeypatch.setattr(wb.requests, "get", lambda url, **k: _Head())   # the PR's current head
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


def test_approved_blocking_finding_turns_the_status_green(monkeypatch, posted):
    # This is the human decision closing the loop: the same function that
    # turned the check red is what turns it green again.
    _stub_alerts(monkeypatch, [_alert(status="resolved", resolution="approved", fragment_key="k")])
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


def test_no_head_anywhere_is_skipped_rather_than_guessed(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])

    def _down(url, **k):
        raise wb.requests.exceptions.ConnectionError("github unreachable")

    monkeypatch.setattr(wb.requests, "get", _down)
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


# --- what a decision means for the commit status ---


def test_a_rejected_finding_keeps_the_status_failing(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert(status="resolved", resolution="rejected", fragment_key="k")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["state"] == "failure"
    assert "rejected" in posted[0]["body"]["description"]


def test_a_rejected_review_only_finding_also_keeps_the_status_failing(monkeypatch, posted):
    # Reject says the PR is not good enough, whatever severity Triage gave it.
    _stub_alerts(monkeypatch, [_alert(severity=SEVERITY_REVIEW, status="resolved", resolution="rejected", fragment_key="k")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["state"] == "failure"


def test_a_rejected_finding_whose_code_is_gone_no_longer_blocks(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert(status="resolved", resolution="rejected", fragment_key="k", unblocked_at="2026-10-17T00:00:00+00:00")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["state"] == "success"


def test_a_rejection_recorded_before_fragment_keys_existed_stays_cleared(monkeypatch, posted):
    # The old rule cleared every resolved finding. Rows from that time have nothing to compare a
    # later commit against, so they must not start blocking retroactively.
    _stub_alerts(monkeypatch, [_alert(status="resolved", resolution="rejected", fragment_key="")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["state"] == "success"


def test_waiting_and_rejected_findings_are_both_counted(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert("w"), _alert("r", status="resolved", resolution="rejected", fragment_key="k")])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["body"]["description"] == "1 finding needs a decision; 1 finding rejected, fix the code"


def test_derive_commit_status_is_one_function_for_every_caller():
    waiting = wb.derive_commit_status([_alert()])
    assert (waiting.state, wb.derive_commit_status([]).state) == ("failure", "success")


# --- which commit ---


def test_the_status_goes_to_the_current_head_not_the_one_stored_on_the_alert(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])

    class _Head:
        def raise_for_status(self): return None
        def json(self): return {"head": {"sha": "newhead9"}}

    monkeypatch.setattr(wb.requests, "get", lambda url, **k: _Head())
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")      # abc123 is what the alert stored
    assert posted[0]["url"].endswith("/statuses/newhead9")


def test_if_the_head_cannot_be_read_the_stored_sha_is_used_and_it_is_logged(monkeypatch, posted, caplog):
    _stub_alerts(monkeypatch, [_alert()])

    def _down(url, **k):
        raise wb.requests.exceptions.ConnectionError("github unreachable")

    monkeypatch.setattr(wb.requests, "get", _down)
    with caplog.at_level("WARNING"):
        wb.sync_pr_check("pandayv/micro-finance", 7, "abc123")
    assert posted[0]["url"].endswith("/statuses/abc123")
    assert "using the stored sha" in caplog.text


def test_the_webhook_can_post_to_the_head_github_just_told_it_about_without_asking_again(monkeypatch, posted):
    def _must_not_ask(url, **k):
        raise AssertionError("no lookup needed")

    monkeypatch.setattr(wb.requests, "get", _must_not_ask)
    assert wb.post_commit_status("pandayv/micro-finance", 7, "pending", "SHIP is reviewing this commit", "fresh1", trust_head=True)
    assert posted[0]["url"].endswith("/statuses/fresh1") and posted[0]["body"]["state"] == "pending"


def test_a_check_for_a_review_still_running_is_pending_not_green(monkeypatch, posted):
    _stub_alerts(monkeypatch, [])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123", final=False)
    assert posted[0]["body"]["state"] == "pending"


def test_a_failure_is_posted_even_while_the_review_is_still_running(monkeypatch, posted):
    _stub_alerts(monkeypatch, [_alert()])
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123", final=False)
    assert posted[0]["body"]["state"] == "failure"


def test_a_caller_that_just_read_the_gate_alerts_does_not_make_the_status_read_again(monkeypatch, posted):
    def _must_not_scan(repo, pr):
        raise AssertionError("already read")

    monkeypatch.setattr(wb, "list_alerts_for_pr", _must_not_scan)
    other_pr = _alert("other")
    other_pr.pr_number = 99
    wb.sync_pr_check("pandayv/micro-finance", 7, "abc123", alerts=[_alert("mine"), other_pr])
    assert posted[0]["body"]["description"].startswith("1 finding")        # only this PR's


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
    assert "no longer blocks the merge" in body and "nothing else is blocking" in body
    assert "set to passing" not in body        # other findings may still block


def test_rejecting_a_finding_says_the_pr_stays_blocked_until_the_code_is_fixed():
    body = wb.comment_for_decision(
        _alert(), accepted=False, reason="Real problem, allowlist landing this sprint.", who="Vipul")
    assert "Rejected" in body and "code fix" in body
    assert "stays failing" in body and "merge stays blocked until a commit fixes this code" in body
