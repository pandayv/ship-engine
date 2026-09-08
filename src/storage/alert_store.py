"""
DynamoDB persistence for alerts Triage has frozen — this is what Attending
reads from. Live-tested against the real table (2026-09-04).

Table schema (single table, on-demand billing to avoid provisioning
decisions during a hackathon sprint):
  - Partition key: alert_id (string) — see put_alert()'s docstring for how
    this is derived (deterministic, not a random UUID, as of the 2026-09-05
    architecture-review fix pass)
  - Attributes: repo, pr_number, file, taxonomy_id, risk_score,
    plain_english_summary, citation, remediation_patch, status
    ("frozen" | "resolved"), severity ("blocking" | "review"), resolution
    ("approved" | "rejected" | None), created_at, resolved_at (ISO 8601
    strings)

Note on status vs. severity: they answer different questions and both are
needed. `status` is where the alert is in its lifecycle (does it still
need a human?). `severity` is what Triage decided the finding warrants
(does it stop the merge, or is it a flag someone should look at while the
build continues?) — see src/agents/triage.py's three routing bands.
"""

import hashlib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

TABLE_NAME = "ship-alerts"

SEVERITY_BLOCKING = "blocking"  # Triage froze the build; merge should not proceed
SEVERITY_REVIEW = "review"      # flagged for a human, but the build was not stopped


@dataclass
class Alert:
    alert_id: str
    repo: str
    pr_number: int
    file: str
    taxonomy_id: str
    risk_score: float
    plain_english_summary: str
    citation: str
    remediation_patch: str
    status: str  # "frozen" | "resolved"
    created_at: str
    resolution: str | None = None  # "approved" | "rejected"
    resolved_at: str | None = None
    # "blocking" defaults here rather than being required because every row
    # written before this field existed (2026-09-07) was a FREEZE, and
    # because a call site that forgets to pass it should fail toward
    # over-blocking, not toward silently letting a blocking finding through.
    severity: str = SEVERITY_BLOCKING
    # The commit this finding was judged against. Stored because the
    # dashboard needs it to update the PR's commit status when a human
    # resolves the alert, long after the webhook payload that carried it
    # is gone. Empty for rows written before this field existed, and for
    # local-test runs that never had a real commit.
    head_sha: str = ""


# finding #16/#50: this used to build a fresh boto3 DynamoDB resource +
# Table wrapper on every single call — real, repeated construction overhead
# inside process_pr()'s hot per-fragment loop, where a multi-fragment PR
# can call put_alert() several times in one invocation. lru_cache(maxsize=1)
# gives the same "build once, reuse" behavior as a hand-rolled singleton
# (finding #35's motivation too) with real failure-tolerance for free:
# lru_cache does NOT cache an exception, so a transient failure on the
# first call doesn't poison every later call in the same process — the
# next call just retries construction, the same guarantee finding #10's
# fix for _get_store() had to hand-write.
@lru_cache(maxsize=1)
def _table():
    import boto3  # lazy import, same reasoning as vector_store.py

    return boto3.resource("dynamodb").Table(TABLE_NAME)


def create_table_if_not_exists() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    try:
        client.describe_table(TableName=TABLE_NAME)
        return  # already exists
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "alert_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "alert_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def _deterministic_alert_id(repo: str, pr_number: int, file: str, taxonomy_id: str, fragment_text: str) -> str:
    """
    Review finding #6: alert_id used to be a fresh uuid4() on every call, so
    a GitHub webhook redelivery or a Lambda async-invoke retry (finding #52)
    re-processing the same PR wrote a brand-new alert for the identical
    violation every time — the dashboard accumulated duplicate frozen rows
    that had to be manually reconciled, and resolving one had no effect on
    the others. Deriving the id from what actually identifies "this
    violation" means a retry's put_item naturally overwrites the same row
    instead of creating a new one — true idempotency, not just
    retry-avoidance.

    Bug an independent review caught (2026-09-05) in the original fix:
    (repo, pr_number, file, taxonomy_id) alone is too coarse. Two entirely
    different violations of the SAME taxonomy_id in the SAME file (e.g. an
    SSN leak in one function and a separate account-number leak in another
    function, both PIIE-001) hashed to the IDENTICAL alert_id — the second
    put_alert() call would silently overwrite the first violation's row
    (the idempotent-write ConditionExpression allows overwriting a still-
    "frozen" row), and once resolved, that same collision would silently
    block a genuinely new future violation of the same taxonomy_id/file
    from ever being persisted at all, with put_alert() returning as if it
    had succeeded. Fixed by including the isolated fragment's own text in
    the key: a real retry of the SAME violation re-diagnoses the SAME
    fragment text (still collides/overwrites correctly, preserving finding
    #6's idempotency guarantee), while two DIFFERENT violations produce
    different fragment text and therefore different ids.
    """
    key = f"{repo}#{pr_number}#{file}#{taxonomy_id}#{fragment_text}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


# finding #27: float<->Decimal conversion used to be hand-written per
# field in two separate functions (put_alert() encoding risk_score alone;
# list_active_alerts() decoding risk_score and pr_number separately) — a
# new numeric field added to Alert needed matching manual lines in both
# places, silently breaking (native float rejected by DynamoDB on write,
# or a stray Decimal leaking into JSON serialization on read) if either
# side was forgotten. Generalized once here, driven off Alert's own field
# type annotations, so it can't drift out of sync with the dataclass.
_FLOAT_FIELDS = {f.name for f in fields(Alert) if f.type is float}
_INT_FIELDS = {f.name for f in fields(Alert) if f.type is int}


def _encode_item(alert: Alert) -> dict:
    item = asdict(alert)
    for name in _FLOAT_FIELDS:
        item[name] = Decimal(str(item[name]))  # DynamoDB rejects native float
    return item


def _decode_item(item: dict) -> dict:
    item = dict(item)
    for name in _FLOAT_FIELDS:
        if name in item:
            item[name] = float(item[name])
    for name in _INT_FIELDS:
        if name in item:
            item[name] = int(item[name])
    return item


def put_alert(repo: str, pr_number: int, file: str, taxonomy_id: str, fragment_text: str, risk_score: float,
              plain_english_summary: str, citation: str, remediation_patch: str,
              severity: str = SEVERITY_BLOCKING, head_sha: str = "") -> Alert:
    # severity is deliberately NOT part of _deterministic_alert_id: a
    # re-diagnosis of the same violation that scores differently enough to
    # cross a band boundary must UPDATE that violation's existing row, not
    # create a second row for the same finding at a different severity.
    alert = Alert(
        alert_id=_deterministic_alert_id(repo, pr_number, file, taxonomy_id, fragment_text),
        repo=repo,
        pr_number=pr_number,
        file=file,
        taxonomy_id=taxonomy_id,
        risk_score=risk_score,
        plain_english_summary=plain_english_summary,
        citation=citation,
        remediation_patch=remediation_patch,
        status="frozen",
        created_at=datetime.now(timezone.utc).isoformat(),
        severity=severity,
        head_sha=head_sha,
    )
    item = _encode_item(alert)
    try:
        # Guard the idempotent write itself: a retry/redelivery for a
        # violation a human already resolved must NOT silently re-freeze
        # it and erase their decision. Only write if the row doesn't exist
        # yet, or still is (unresolved).
        _table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(alert_id) OR #s = :frozen",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":frozen": "frozen"},
        )
    except Exception as e:
        if type(e).__name__ == "ConditionalCheckFailedException":
            # Already resolved by a human — a retry re-diagnosing the same
            # violation is not an error, just a no-op on the stored state.
            return alert
        raise
    return alert


def _scan_all(**scan_kwargs) -> list[dict]:
    # finding #14: DynamoDB Scan reads at most 1MB BEFORE applying
    # FilterExpression, then returns LastEvaluatedKey if more of the table
    # remains to be scanned - a single, unpaginated .scan() silently
    # truncates once the table grows past that, and a reviewer sees an
    # empty-looking queue while real "frozen" alerts sit unseen past the
    # 1MB boundary. Loop until LastEvaluatedKey is actually absent.
    table = _table()
    items = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key


def _row_to_alert(item: dict) -> Alert:
    """
    One place where a stored row becomes an Alert, so every reader gets
    the same schema-evolution tolerance. Adding a field to Alert means
    adding its historical default here and nowhere else.
    """
    # DynamoDB's Number type has no int/float distinction — everything
    # numeric comes back as Decimal; _decode_item() converts back to what
    # the rest of the app (and JSON serialization) actually expects,
    # generically off Alert's own field types (finding #27).
    item = _decode_item(item)
    # Tolerate rows written before a field existed (caught live,
    # 2026-09-05: real rows from earlier that session predate the "file"
    # field added for finding #60 and crashed with a missing-argument
    # TypeError).
    item.setdefault("file", "<unknown>")
    # Rows predating severity were written by the old binary
    # freeze-or-nothing path, so "blocking" is their historically correct
    # value, not just a safe filler.
    item.setdefault("severity", SEVERITY_BLOCKING)
    # Rows predating head_sha have no commit to post a status against;
    # the write-back path skips an empty sha rather than guessing.
    item.setdefault("head_sha", "")
    return Alert(**item)


def list_active_alerts() -> list[Alert]:
    items = _scan_all(
        FilterExpression="#s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "frozen"},
    )
    return [_row_to_alert(i) for i in items]


def list_alerts_for_pr(repo: str, pr_number: int) -> list[Alert]:
    """
    Every alert for one PR, resolved ones included — the caller needs the
    resolved rows to tell "decided and cleared" from "never found anything"
    (src/api/github_writeback.py's sync_pr_check filters on status itself).

    A Scan with a filter rather than a Query because alert_id is the only
    key on this table. Correct, and fine at the scale this runs at; a
    repo+pr global secondary index is the change to make if the table ever
    grows past a scan being cheap.
    """
    # Both names aliased rather than inlined: DynamoDB's reserved-word list
    # is long and a collision fails at runtime, not at write time.
    items = _scan_all(
        FilterExpression="#r = :repo AND #p = :pr",
        ExpressionAttributeNames={"#r": "repo", "#p": "pr_number"},
        ExpressionAttributeValues={":repo": repo, ":pr": pr_number},
    )
    return [_row_to_alert(i) for i in items]


def get_alert(alert_id: str) -> Alert | None:
    item = _table().get_item(Key={"alert_id": alert_id}).get("Item")
    return _row_to_alert(item) if item else None


class AlertNotFoundOrAlreadyResolved(Exception):
    """Raised when resolve_alert() targets an alert_id that doesn't exist,
    or that's already been resolved. Review findings #3/#4: DynamoDB's
    update_item upserts by default with no existence check, so a bogus or
    stale alert_id used to silently create a malformed ghost row instead of
    404ing, and an already-resolved alert could be silently re-resolved
    (overwriting a prior rejection with an approval, or vice versa) with no
    conflict detection and no record the first decision ever happened."""


def resolve_alert(alert_id: str, approved: bool) -> None:
    try:
        _table().update_item(
            Key={"alert_id": alert_id},
            UpdateExpression="SET #s = :status, resolution = :resolution, resolved_at = :resolved_at",
            ConditionExpression="attribute_exists(alert_id) AND #s = :frozen",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "resolved",
                ":resolution": "approved" if approved else "rejected",
                ":resolved_at": datetime.now(timezone.utc).isoformat(),
                ":frozen": "frozen",
            },
        )
    except Exception as e:
        if type(e).__name__ == "ConditionalCheckFailedException":
            raise AlertNotFoundOrAlreadyResolved(
                f"alert_id {alert_id!r} does not exist or is not currently frozen"
            ) from e
        raise


if __name__ == "__main__":
    # will only run once dynamodb:* permissions are added to ship-agent's policy
    create_table_if_not_exists()
    alert = put_alert(
        repo="pandayv/micro-finance", pr_number=1, file="loans/ai_underwriting.py",
        taxonomy_id="PIIE-001", fragment_text="prompt = f'SSN {applicant.ssn}'", risk_score=9.1,
        plain_english_summary="Raw applicant PII sent to an external LLM without masking.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash/redact PII before building the prompt",
    )
    print("Wrote alert:", alert)
    print("Active alerts:", list_active_alerts())
    resolve_alert(alert.alert_id, approved=True)
    print("Resolved. Active alerts now:", list_active_alerts())
