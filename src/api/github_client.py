"""
Fetches a pull request's diff from the GitHub REST API. A webhook event
payload carries PR metadata (number, repo) but not the full diff text
itself — this closes that gap, replacing the earlier `{"code_diff": "..."}`
placeholder used for local testing.
"""

import os

import requests

GITHUB_API_BASE = "https://api.github.com"


def fetch_pr_diff(repo_full_name: str, pr_number: int, token: str | None = None) -> str:
    """
    repo_full_name: e.g. "pandayv/micro-finance"
    token: a GitHub token with repo read access. Falls back to the
    GITHUB_TOKEN env var if not passed explicitly. Public repos can be
    fetched unauthenticated too, but with much lower rate limits.
    """
    token = token or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github.v3.diff"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}"
    response = requests.get(url, headers=headers, timeout=10)
    response.raise_for_status()
    return response.text


def is_pr_event(payload: dict) -> bool:
    """True if this looks like a real GitHub pull_request webhook payload
    (as opposed to our simplified local-testing {"code_diff": ...} body)."""
    return (
        payload.get("action") in {"opened", "synchronize", "reopened", "edited"}
        and "pull_request" in payload
        and "repository" in payload
    )


def extract_pr_ref(payload: dict) -> tuple[str, int]:
    repo_full_name = payload["repository"]["full_name"]
    pr_number = payload["pull_request"]["number"]
    return repo_full_name, pr_number


if __name__ == "__main__":
    # smoke test against the real, already-open PR #1
    import subprocess

    token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    diff = fetch_pr_diff("pandayv/micro-finance", 1, token=token)
    print(f"Fetched {len(diff)} chars of real diff via the GitHub REST API (not `gh pr diff`):")
    print(diff[:300])
