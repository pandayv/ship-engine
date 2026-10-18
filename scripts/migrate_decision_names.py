#!/usr/bin/env python3
"""
One-time migration: the decision stored on a resolved finding is now "allowed" or
"blocked". Rows decided earlier hold "approved" or "rejected".

    python scripts/migrate_decision_names.py            # dry run: prints what would change
    python scripts/migrate_decision_names.py --apply    # writes it

Only the `resolution` attribute of rows in the ship-alerts table is touched. It is safe
to run twice: each write is conditional on the row still holding the old value, so a
row that is already migrated (or was changed by someone in the meantime) is left alone,
and the second run finds nothing to do. Run it after the new code is deployed: until
then the old code only understands the old names.

Staged decisions in ship-pending-dispositions are not migrated. They expire after ten
minutes, and the new code refuses to apply one it cannot read (the person is asked to
say it again).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):      # run as a file: the repository root is what makes `src` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.alert_store import RESOLUTION_ALLOWED, RESOLUTION_BLOCKED, TABLE_NAME, _is_conditional_failure

RENAMES = {"approved": RESOLUTION_ALLOWED, "rejected": RESOLUTION_BLOCKED}


def find_rows(table) -> list[dict]:
    """Every row still holding an old decision name, read strongly consistent and across
    every page of the scan."""
    kwargs = {
        "FilterExpression": "#res = :approved OR #res = :rejected",
        "ExpressionAttributeNames": {"#res": "resolution"},
        "ExpressionAttributeValues": {":approved": "approved", ":rejected": "rejected"},
        "ConsistentRead": True,
    }
    rows = []
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if not page.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def migrate(table, apply: bool = False, out=print) -> dict:
    """Prints one line per row that would change (or did). Returns the counts."""
    changed = skipped = 0
    for row in sorted(find_rows(table), key=lambda r: r["alert_id"]):
        old = row["resolution"]
        new = RENAMES[old]
        where = f"{row.get('repo', '?')}#{row.get('pr_number', '?')}"
        if not apply:
            out(f"would change {row['alert_id']} ({where}): {old} -> {new}")
            changed += 1
            continue
        try:
            table.update_item(
                Key={"alert_id": row["alert_id"]},
                UpdateExpression="SET #res = :new",
                ConditionExpression="#res = :old",
                ExpressionAttributeNames={"#res": "resolution"},
                ExpressionAttributeValues={":new": new, ":old": old},
            )
            out(f"changed {row['alert_id']} ({where}): {old} -> {new}")
            changed += 1
        except Exception as exc:  # noqa: BLE001
            if not _is_conditional_failure(exc):
                raise
            out(f"left {row['alert_id']} ({where}): no longer {old}")
            skipped += 1
    verb = "changed" if apply else "would change"
    out(f"{verb} {changed} row(s) in {TABLE_NAME}" + (f", left {skipped} alone" if skipped else "")
        + ("" if apply else ". Dry run: nothing was written. Run again with --apply to write."))
    return {"changed": changed, "skipped": skipped, "applied": apply}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    args = parser.parse_args(argv)

    import boto3

    migrate(boto3.resource("dynamodb").Table(TABLE_NAME), apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
