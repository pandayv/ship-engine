"""
A finding is posted to the pull request once, and the person is told once, even when a step
fails or the invocation dies part way.

Found by the 2026-10-08 smoke test: a second commit pushed to a PR re-detected a finding a
person had already accepted, and SHIP commented "merge blocked" again on a PR whose check was
green. The first fix tied "comment" to the creation of the row. That left a hole: a failed
comment POST, or a crash after the row was written, meant an SQS retry saw "not new" and the
comment and the Echo notification were never sent. Now each delivery step is recorded on the
alert only after it succeeds, under a short lease, and every pass finishes whichever step is
missing without repeating the one already done.
"""

import pytest

from src.agents.detector import DetectorOutput
from src.api.main import process_fragment
from src.storage import alert_store
from tests.fake_dynamo import FakeTable


@pytest.fixture
def table(monkeypatch):
    fake = FakeTable("alert_id")
    monkeypatch.setattr(alert_store, "_table", lambda: fake)
    return fake


def _put(**over):
    args = dict(repo="pandayv/micro-finance", pr_number=4, file="loans/a.py", taxonomy_id="PIIE-001",
                fragment_text="prompt = f'{c.email}'", risk_score=8.5, plain_english_summary="s",
                citation="GDPR Article 32", remediation_patch="p", head_sha="sha1")
    args.update(over)
    return alert_store.put_alert(**args)


# --- the row ---


def test_a_first_detection_is_reported_as_new(table):
    alert = _put()
    assert alert.newly_written is True and alert.status == "frozen" and alert.fragment_key


def test_re_detecting_a_finding_that_is_still_open_is_not_new_and_keeps_its_bookkeeping(table):
    first = _put()
    alert_store.complete_step(first.alert_id, "comment")
    again = _put(risk_score=9.4, head_sha="sha2")
    assert again.newly_written is False
    assert again.commented_at                                  # not erased by the second pass
    assert again.risk_score == 9.4 and again.head_sha == "sha2"      # the verdict is refreshed while open
    assert again.created_at == first.created_at


def test_re_detecting_a_finding_a_person_already_resolved_changes_nothing(table):
    first = _put()
    alert_store.resolve_alert(first.alert_id, approved=True, reason="fixture", resolved_by="x")
    again = _put(risk_score=2.0)
    assert again.newly_written is False and again.status == "resolved" and again.resolution == "approved"
    assert table.rows[first.alert_id]["risk_score"] != 2.0


def test_unset_bookkeeping_is_not_stored_as_null(table):
    alert = _put()
    row = table.rows[alert.alert_id]
    for name in ("commented_at", "notified_at", "comment_claimed_at", "notify_claimed_at", "unblocked_at", "newly_written"):
        assert name not in row, name


# --- claiming a step ---


def test_a_step_can_be_claimed_once_while_it_is_in_flight(table):
    alert = _put()
    assert alert_store.claim_step(alert.alert_id, "comment") is True
    assert alert_store.claim_step(alert.alert_id, "comment") is False     # another invocation is mid-send
    assert alert_store.claim_step(alert.alert_id, "notify") is True       # steps are independent


def test_a_step_that_was_sent_is_never_claimed_again(table):
    alert = _put()
    assert alert_store.claim_step(alert.alert_id, "comment")
    alert_store.complete_step(alert.alert_id, "comment")
    assert alert_store.claim_step(alert.alert_id, "comment") is False
    assert "comment_claimed_at" not in table.rows[alert.alert_id]


def test_a_claim_whose_invocation_died_can_be_taken_over_after_the_lease(table, monkeypatch):
    alert = _put()
    assert alert_store.claim_step(alert.alert_id, "comment")
    table.rows[alert.alert_id]["comment_claimed_at"] -= alert_store.CLAIM_LEASE_SECONDS + 1
    assert alert_store.claim_step(alert.alert_id, "comment") is True


def test_a_released_claim_can_be_taken_at_once(table):
    alert = _put()
    alert_store.claim_step(alert.alert_id, "comment")
    alert_store.release_step(alert.alert_id, "comment")
    assert alert_store.claim_step(alert.alert_id, "comment") is True


def test_claiming_a_finding_that_does_not_exist_is_refused(table):
    assert alert_store.claim_step("nope", "comment") is False


# --- the pipeline ---


@pytest.fixture
def pipeline(monkeypatch, table):
    out = {"comments": [], "notes": [], "checks": [], "post_ok": True}

    def post(repo, pr, body):
        out["comments"].append(body)
        return out["post_ok"]

    monkeypatch.setattr("src.api.main.detect", lambda text: DetectorOutput(
        matched=True, taxonomy_id="PIIE-001", risk_score=8.5,
        plain_english_summary="s", citation="c", remediation_patch="p"))
    monkeypatch.setattr("src.api.main.post_pr_comment", post)
    monkeypatch.setattr("src.api.main.sync_pr_check", lambda *a, **k: out["checks"].append((a, k)) or True)
    monkeypatch.setattr("src.api.main.comment_for_finding", lambda alert: "comment")
    monkeypatch.setattr("src.api.main.refresh_summary", lambda *a, **k: None)
    monkeypatch.setattr("src.api.main.mark_reviewed", lambda repo: None)
    monkeypatch.setattr("src.api.main.notify_new_finding", lambda alert_id, n: out["notes"].append(alert_id) or 1)
    monkeypatch.setattr("src.api.main.fragment_reported", lambda *a: True)
    return out


def run(sha="sha1"):
    return process_fragment("pandayv/micro-finance", 4, "loans/a.py", "frag", sha)


def test_a_new_finding_is_commented_on_the_pull_request_and_the_person_is_told(pipeline):
    run()
    assert len(pipeline["comments"]) == 1 and len(pipeline["notes"]) == 1


def test_a_second_push_that_re_detects_the_finding_neither_comments_nor_chimes_again(pipeline):
    run("sha1")
    run("sha2")
    assert len(pipeline["comments"]) == 1 and len(pipeline["notes"]) == 1


def test_a_finding_a_person_already_decided_is_not_commented_or_announced_again(pipeline, table):
    first = run()
    alert_store.resolve_alert(first["alert_id"], approved=True, reason="fixture", resolved_by="x")
    run("sha2")
    assert len(pipeline["comments"]) == 1 and len(pipeline["notes"]) == 1


def test_the_check_is_recomputed_even_when_nothing_new_is_posted(pipeline):
    run("sha1")
    run("sha2")
    assert len(pipeline["checks"]) == 2


def test_a_failed_comment_post_is_finished_by_the_next_pass_without_chiming_twice(pipeline):
    pipeline["post_ok"] = False
    run("sha1")
    assert len(pipeline["comments"]) == 1 and len(pipeline["notes"]) == 1       # comment tried, chime sent

    pipeline["post_ok"] = True
    run("sha1")                                                                 # the SQS retry
    assert len(pipeline["comments"]) == 2           # retried, and this time recorded
    assert len(pipeline["notes"]) == 1              # the chime that already went out is not repeated

    run("sha1")
    assert len(pipeline["comments"]) == 2           # now it stays done


def test_a_crash_after_the_row_was_written_loses_neither_the_comment_nor_the_chime(pipeline, monkeypatch, table):
    # The invocation dies right after put_alert: nothing was sent and nothing recorded.
    alert = _put(fragment_text="frag", file="loans/a.py", head_sha="sha1")
    assert not alert.commented_at and not alert.notified_at

    run("sha1")                                                                 # the SQS redelivery
    assert len(pipeline["comments"]) == 1 and len(pipeline["notes"]) == 1


def test_a_crash_between_the_comment_and_the_chime_sends_only_the_chime_on_retry(pipeline, table):
    alert = _put(fragment_text="frag", file="loans/a.py", head_sha="sha1")
    alert_store.claim_step(alert.alert_id, "comment")
    alert_store.complete_step(alert.alert_id, "comment")                        # comment went out, then the crash

    run("sha1")
    assert pipeline["comments"] == [] and len(pipeline["notes"]) == 1


def test_a_crash_between_the_chime_and_recording_it_repeats_it_at_most_once_after_the_lease(pipeline, table):
    alert = _put(fragment_text="frag", file="loans/a.py", head_sha="sha1")
    alert_store.claim_step(alert.alert_id, "notify")                            # chime sent, died before recording
    run("sha1")
    assert pipeline["notes"] == []                                              # lease still held: no duplicate yet
    table.rows[alert.alert_id]["notify_claimed_at"] -= alert_store.CLAIM_LEASE_SECONDS + 1
    run("sha1")
    assert len(pipeline["notes"]) == 1                                          # not lost


def test_a_chime_nobody_received_is_tried_again_on_the_next_pass(pipeline, monkeypatch):
    monkeypatch.setattr("src.api.main.notify_new_finding", lambda alert_id, n: pipeline["notes"].append(alert_id) or 0)
    run("sha1")
    run("sha1")
    assert len(pipeline["notes"]) == 2


def test_two_passes_at_the_same_time_send_the_comment_once(pipeline, table):
    alert = _put(fragment_text="frag", file="loans/a.py", head_sha="sha1")
    assert alert_store.claim_step(alert.alert_id, "comment") is True            # first invocation is mid-send
    run("sha1")                                                                 # a second one arrives
    assert pipeline["comments"] == []
