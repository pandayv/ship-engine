from src.api.github_client import extract_pr_ref, is_pr_event

# Trimmed but structurally real shape of a GitHub pull_request webhook event
REAL_PR_EVENT = {
    "action": "opened",
    "number": 1,
    "pull_request": {"number": 1, "title": "Add AI-assisted underwriting opinion"},
    "repository": {"full_name": "pandayv/micro-finance"},
}

SIMPLIFIED_LOCAL_TEST_PAYLOAD = {"code_diff": "import openai\n..."}


def test_recognizes_real_pr_event():
    assert is_pr_event(REAL_PR_EVENT) is True


def test_does_not_misclassify_simplified_local_payload():
    assert is_pr_event(SIMPLIFIED_LOCAL_TEST_PAYLOAD) is False


def test_rejects_closed_action():
    closed = {**REAL_PR_EVENT, "action": "closed"}
    assert is_pr_event(closed) is False


def test_extract_pr_ref():
    repo, number = extract_pr_ref(REAL_PR_EVENT)
    assert repo == "pandayv/micro-finance"
    assert number == 1
