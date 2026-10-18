"""
Every commit the webhook accepts ends in a real status on the pull request. A required check can
be satisfied by a status, so absence must never be able to pass: "pending" the moment the commit
is accepted, then success/failure from the review, or "error" where a review could not run.
"""

import hashlib
import hmac
import json

import pytest
import requests
from fastapi.testclient import TestClient

import src.api.main as main_module
from src.api.main import app

EVENT = {
    "action": "synchronize",
    "pull_request": {"number": 7, "head": {"sha": "newsha"}},
    "repository": {"full_name": "pandayv/micro-finance"},
}
FLAGGED_DIFF = '''diff --git a/loans/apply.py b/loans/apply.py
--- a/loans/apply.py
+++ b/loans/apply.py
@@ -1,3 +1,4 @@
+def process_application(applicant):
+    logger.info(f"SSN: {applicant.ssn}")
'''
CLEAN_DIFF = '''diff --git a/loans/clean.py b/loans/clean.py
--- a/loans/clean.py
+++ b/loans/clean.py
@@ -1,3 +1,4 @@
+def calculate_payment(principal, rate):
+    return principal * rate
'''


@pytest.fixture
def hook(monkeypatch):
    log = {"statuses": [], "syncs": [], "released": [], "dispatched": [], "watch": "watched"}
    monkeypatch.setattr(main_module, "GITHUB_WEBHOOK_SECRET", "s")
    monkeypatch.setattr(main_module, "is_watched", lambda repo: log["watch"] == "watched")
    monkeypatch.setattr(main_module, "watch_state", lambda repo: log["watch"])
    monkeypatch.setattr(main_module, "mark_event_seen", lambda repo: None)
    monkeypatch.setattr(main_module, "mark_reviewed", lambda repo: None)
    monkeypatch.setattr(main_module, "refresh_summary", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "fetch_pr_diff", lambda repo, pr: log["diff"])
    monkeypatch.setattr(main_module, "post_commit_status", lambda repo, pr, state, desc, sha="", trust_head=False:
                        log["statuses"].append((state, desc, sha, trust_head)) or True)
    monkeypatch.setattr(main_module, "sync_pr_check", lambda *a, **k: log["syncs"].append(a) or True)
    monkeypatch.setattr(main_module, "release_fixed_rejections", lambda repo, pr, keys: log["released"].append(set(keys)) or 0)
    monkeypatch.setattr(main_module, "_dispatch_fragments",
                        lambda repo, pr, diff, sha: log["dispatched"].append((repo, pr, sha)) or {"action": "accepted"})
    log["diff"] = FLAGGED_DIFF
    log["client"] = TestClient(app)
    return log


def send(hook, payload=EVENT):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    return hook["client"].post("/api/v1/webhook", content=body,
                               headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def test_an_accepted_commit_is_marked_pending_on_the_head_github_named_before_any_work(hook):
    send(hook)
    assert hook["statuses"][0] == ("pending", "SHIP is reviewing this commit", "newsha", True)


def test_a_commit_with_nothing_flagged_replaces_the_pending_status_with_the_verdict(hook):
    hook["diff"] = CLEAN_DIFF
    assert send(hook).json()["action"] == "pass"
    assert len(hook["syncs"]) == 1 and hook["dispatched"] == []
    assert hook["released"] == [set()]       # nothing flagged any more: rejected findings may be released


def test_an_empty_diff_also_ends_in_a_status(hook):
    hook["diff"] = ""
    send(hook)
    assert len(hook["syncs"]) == 1


def test_a_flagged_commit_is_handed_to_dispatch_which_owns_the_rest(hook):
    send(hook)
    assert hook["dispatched"] == [("pandayv/micro-finance", 7, "newsha")]


def test_a_diff_that_cannot_be_fetched_is_an_error_status_not_silence(hook, monkeypatch):
    def down(repo, pr):
        raise requests.exceptions.ConnectionError("down")

    monkeypatch.setattr(main_module, "fetch_pr_diff", down)
    assert send(hook).status_code == 502
    assert [s[0] for s in hook["statuses"]] == ["pending", "error"]


def test_when_the_allowlist_cannot_be_read_the_error_is_reported_and_logged(hook, caplog):
    hook["watch"] = "unknown"
    with caplog.at_level("ERROR"):
        response = send(hook)
    assert response.status_code == 503
    assert hook["statuses"] == [("error", "SHIP could not verify this repository, so the review did not run", "newsha", True)]
    assert "cannot tell whether" in caplog.text


def test_a_repository_that_is_not_connected_is_refused_without_touching_it(hook):
    hook["watch"] = "not_watched"
    assert send(hook).status_code == 403
    assert hook["statuses"] == []


def test_the_local_test_body_never_posts_or_releases_anything(hook):
    hook["diff"] = CLEAN_DIFF
    body = json.dumps({"code_diff": CLEAN_DIFF}).encode()
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()
    hook["client"].post("/api/v1/webhook", content=body, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert hook["statuses"] == [] and hook["released"] == []
