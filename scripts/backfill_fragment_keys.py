#!/usr/bin/env python3
"""
Gives findings that predate code fingerprints their fingerprint, so a "block" on them holds the
merge.

A blocked finding keeps its pull request blocked until a later commit no longer contains the
flagged code, and "the flagged code" is identified by a fingerprint (alert_store.fragment_key_for)
stored on the finding. Findings recorded before fingerprints existed have none, so blocking one
would have no effect on the merge.

This recomputes each fingerprint exactly as the webhook does, from the pull request's diff, and
matches it to the stored finding by its id (the id is derived from the same code and the
finding's category). A finding that no longer matches any fragment of the current diff (the code
changed since it was found) gets a marker key that no commit will ever contain, so a block on it
holds until the next commit is reviewed and then releases, which is the nearest honest answer when
the original code is not recoverable.

Dry run by default. `--apply` writes. Only findings with no fingerprint are touched, each with a
conditional update, so a second run changes nothing.

    python scripts/backfill_fragment_keys.py               # show what would change
    python scripts/backfill_fragment_keys.py --apply
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TABLE = "ship-alerts"
UNMATCHED_PREFIX = "unmatched:"


def _scan_all(table):
    items, kwargs = [], {"ConsistentRead": True}
    while True:
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _pull_request_diff(repo: str, pr_number: int) -> str:
    """The pull request's current diff, read with the GitHub CLI (read access is enough)."""
    return subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "-H", "Accept: application/vnd.github.diff"],
        check=True, capture_output=True, text=True,
    ).stdout


def plan(items, diff_for):
    """[(alert_id, repo, pr, fragment_key, how)] for each finding that has no fingerprint.
    `diff_for(repo, pr)` returns the pull request's diff text (None if it cannot be read)."""
    from src.api.main import _escalate
    from src.storage.alert_store import _deterministic_alert_id

    changes, escalated_cache = [], {}
    for row in sorted(items, key=lambda r: (r["repo"], int(r["pr_number"]), r["created_at"])):
        if row.get("fragment_key"):
            continue
        repo, pr = row["repo"], int(row["pr_number"])
        if (repo, pr) not in escalated_cache:
            diff = diff_for(repo, pr)
            escalated_cache[(repo, pr)] = _escalate(diff)[1] if diff else []
        key = None
        for frag in escalated_cache[(repo, pr)]:
            if frag["file"] != row["file"]:
                continue
            if _deterministic_alert_id(repo, pr, frag["file"], row["taxonomy_id"], frag["isolated_fragment"]) == row["alert_id"]:
                key = frag["fragment_key"]
                break
        if key:
            changes.append((row["alert_id"], repo, pr, key, "matched the current diff"))
        else:
            changes.append((row["alert_id"], repo, pr, UNMATCHED_PREFIX + row["alert_id"][:24], "code no longer in the diff"))
    return changes


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the fingerprints (default: dry run)")
    args = parser.parse_args(argv)

    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb", region_name="us-west-2").Table(TABLE)
    changes = plan(_scan_all(table), _pull_request_diff)
    if not changes:
        print("every finding already has a fingerprint")
        return 0
    done = 0
    for alert_id, repo, pr, key, how in changes:
        print(f"{'set' if args.apply else 'would set'} {alert_id[:8]} ({repo}#{pr}): {key[:12]}... ({how})")
        if not args.apply:
            continue
        try:
            table.update_item(
                Key={"alert_id": alert_id},
                UpdateExpression="SET fragment_key = :k",
                ConditionExpression="attribute_exists(alert_id) AND attribute_not_exists(fragment_key)",
                ExpressionAttributeValues={":k": key},
            )
            done += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
    print(f"{'changed' if args.apply else 'would change'} {done if args.apply else len(changes)} finding(s) in {TABLE}."
          + ("" if args.apply else " Dry run: nothing was written. Run again with --apply to write."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
