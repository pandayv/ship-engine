#!/usr/bin/env python3
"""
Resets the demo: clears SHIP's findings and opens fresh pull requests on the demo repository.

Use it between recording takes, or whenever the demo data has been decided or has drifted. It
does what you would do by hand:

  1. closes the open demo pull requests and deletes their branches;
  2. deletes every finding in ship-alerts, so the voice status starts from zero;
  3. opens one new pull request per demo change, from the same feature branches, each on its own
     commit (two pull requests on one commit would share a check status);
  4. waits for SHIP to review them and prints what it found.

It changes real things (GitHub pull requests, DynamoDB rows), so it only plans unless you pass
--yes. It needs the GitHub CLI signed in as the account that owns the demo repository.

    python scripts/demo_reset.py            # show the plan
    python scripts/demo_reset.py --yes      # do it
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

REGION = "us-west-2"
REPO = "pandayv/micro-finance"
OWNER = "pandayv"          # the account the demo repository belongs to (see the account rules)
TABLE = "ship-alerts"
DEMO_PREFIXES = ("demo/", "e2e/")
DEMOS = [
    ("feature/ai-underwriting-assistant", "demo/ai-underwriting-assistant",
     "Add AI-assisted underwriting opinion for loan approval/rejection",
     "Adds an AI-generated underwriting opinion to the loan decision flow."),
    ("feature/ai-admin-and-risk-tools", "demo/ai-admin-and-risk-tools",
     "Add AI admin assistant, risk scoring, and session caching",
     "Adds an AI admin assistant, risk scoring and session caching."),
]


class Gh:
    """The GitHub CLI, behind one small door so tests can replace it."""

    def run(self, *args: str) -> str:
        out = subprocess.run(["gh", *args], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:3])} failed: {out.stderr.strip()[:200]}")
        return out.stdout

    def json(self, *args: str):
        return json.loads(self.run(*args) or "null")


def active_account(gh: Gh) -> str:
    out = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    lines = (out.stdout + out.stderr).splitlines()
    for i, line in enumerate(lines):
        if "Active account: true" in line:
            for earlier in reversed(lines[:i]):
                if "Logged in to github.com account" in earlier:
                    return earlier.split("account")[1].split()[0]
    return ""


def open_demo_pulls(gh: Gh, repo: str) -> list[dict]:
    pulls = gh.json("pr", "list", "--repo", repo, "--state", "open", "--json", "number,headRefName,title")
    return [p for p in pulls if p["headRefName"].startswith(DEMO_PREFIXES)]


def existing_demo_branches(gh: Gh, repo: str) -> list[str]:
    names = gh.run("api", f"repos/{repo}/branches", "--paginate", "--jq", ".[].name").split()
    return [n for n in names if n.startswith(DEMO_PREFIXES)]


def plan(gh: Gh, repo: str) -> list[str]:
    steps = [f"close pull request #{p['number']} ({p['headRefName']})" for p in open_demo_pulls(gh, repo)]
    steps += [f"delete branch {b}" for b in existing_demo_branches(gh, repo)]
    steps.append(f"delete every finding in {TABLE}")
    steps += [f"open a pull request from {new} (a copy of {src})" for src, new, _, _ in DEMOS]
    return steps


def delete_findings(session) -> int:
    table = session.resource("dynamodb", region_name=REGION).Table(TABLE)
    rows, kwargs = [], {"ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        rows += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    for row in rows:
        table.delete_item(Key={"alert_id": row["alert_id"]})
    return len(rows)


def open_fresh_pull(gh: Gh, repo: str, base: str, source: str, branch: str, title: str, body: str) -> int:
    """A new branch whose single new commit has the same files as `source`, then a pull request."""
    head = gh.run("api", f"repos/{repo}/git/ref/heads/{source}", "--jq", ".object.sha").strip()
    tree = gh.run("api", f"repos/{repo}/git/commits/{head}", "--jq", ".tree.sha").strip()
    commit = gh.run("api", "-X", "POST", f"repos/{repo}/git/commits", "-f", f"message=Demo: {title}",
                    "-f", f"tree={tree}", "-f", f"parents[]={head}", "--jq", ".sha").strip()
    gh.run("api", "-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={commit}")
    url = gh.run("pr", "create", "--repo", repo, "--base", base, "--head", branch, "--title", title, "--body", body).strip()
    return int(url.rstrip("/").split("/")[-1])


def findings_per_pr(session) -> dict[int, int]:
    table = session.resource("dynamodb", region_name=REGION).Table(TABLE)
    counts, kwargs = {}, {"ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        for row in page.get("Items", []):
            counts[int(row["pr_number"])] = counts.get(int(row["pr_number"]), 0) + 1
        if "LastEvaluatedKey" not in page:
            return counts
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def wait_for_review(session, pulls: list[int], timeout: int, say=print) -> dict[int, int]:
    """Until every new pull request has findings and the counts stop changing, or the time is up."""
    deadline, last, stable = time.time() + timeout, None, 0
    while time.time() < deadline:
        counts = findings_per_pr(session)
        say(f"  findings so far: {counts or 'none yet'}")
        if all(counts.get(n) for n in pulls):
            stable = stable + 1 if counts == last else 0
            if stable >= 2:
                return counts
        last = counts
        time.sleep(20)
    return findings_per_pr(session)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="do it (default: only show the plan)")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--wait", type=int, default=420, help="seconds to wait for SHIP to review (default 420)")
    args = parser.parse_args(argv)

    gh = Gh()
    account = active_account(gh)
    if account != OWNER:
        print(f"The active GitHub account is '{account or 'unknown'}', but the demo repository belongs to '{OWNER}'.\n"
              f"Switch with:  gh auth switch --user {OWNER}")
        return 2

    steps = plan(gh, args.repo)
    print("This will:")
    for step in steps:
        print(f"  - {step}")
    if not args.yes:
        print("\nNothing was changed. Run again with --yes to do it.")
        return 0

    import boto3

    session = boto3.Session()
    base = gh.run("repo", "view", args.repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name").strip()
    for pull in open_demo_pulls(gh, args.repo):
        gh.run("pr", "close", str(pull["number"]), "--repo", args.repo, "--comment", "Demo reset: replaced by a fresh pull request.")
        print(f"closed #{pull['number']}")
    for branch in existing_demo_branches(gh, args.repo):
        gh.run("api", "-X", "DELETE", f"repos/{args.repo}/git/refs/heads/{branch}")
        print(f"deleted branch {branch}")
    print(f"deleted {delete_findings(session)} findings")

    new_pulls = []
    for source, branch, title, body in DEMOS:
        number = open_fresh_pull(gh, args.repo, base, source, branch, title, body)
        new_pulls.append(number)
        print(f"opened #{number}: {title}")
    print("\nWaiting for SHIP to review them...")
    counts = wait_for_review(session, new_pulls, args.wait)
    print("\nDemo data:", ", ".join(f"PR #{n}: {counts.get(n, 0)} findings" for n in new_pulls))
    print("Next: python scripts/preflight.py")
    return 0 if all(counts.get(n) for n in new_pulls) else 1


if __name__ == "__main__":
    sys.exit(main())
