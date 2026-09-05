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
    ("frozen" | "resolved"), resolution ("approved" | "rejected" | None),
    created_at, resolved_at (ISO 8601 strings)
"""

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal

TABLE_NAME = "ship-alerts"


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


def _deterministic_alert_id(repo: str, pr_number: int, file: str, taxonomy_id: str) -> str:
    """
    Review finding #6: alert_id used to be a fresh uuid4() on every call, so
    a GitHub webhook redelivery or a Lambda async-invoke retry (finding #52)
    re-processing the same PR wrote a brand-new alert for the identical
    violation every time — the dashboard accumulated duplicate frozen rows
    that had to be manually reconciled, and resolving one had no effect on
    the others. Deriving the id from what actually identifies "this
    violation" (which repo, which PR, which file, which taxonomy category)
    means a retry's put_item naturally overwrites the same row instead of
    creating a new one — true idempotency, not just retry-avoidance.
    """
    key = f"{repo}#{pr_number}#{file}#{taxonomy_id}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def put_alert(repo: str, pr_number: int, file: str, taxonomy_id: str, risk_score: float,
              plain_english_summary: str, citation: str, remediation_patch: str) -> Alert:
    alert = Alert(
        alert_id=_deterministic_alert_id(repo, pr_number, file, taxonomy_id),
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
    )
    item = asdict(alert)
    item["risk_score"] = Decimal(str(item["risk_score"]))  # DynamoDB rejects native float
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


def list_active_alerts() -> list[Alert]:
    # finding #14: DynamoDB Scan reads at most 1MB BEFORE applying
    # FilterExpression, then returns LastEvaluatedKey if more of the table
    # remains to be scanned - a single, unpaginated .scan() silently
    # truncates once the table grows past that, and a reviewer sees an
    # empty-looking queue while real "frozen" alerts sit unseen past the
    # 1MB boundary. Loop until LastEvaluatedKey is actually absent.
    table = _table()
    items = []
    scan_kwargs = {
        "FilterExpression": "#s = :status",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":status": "frozen"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    alerts = []
    for item in items:
        # DynamoDB's Number type has no int/float distinction — everything
        # numeric comes back as Decimal; convert back to what the rest of
        # the app (and JSON serialization) actually expects
        item["risk_score"] = float(item["risk_score"])
        item["pr_number"] = int(item["pr_number"])
        # Tolerate rows written before a field existed (caught live,
        # 2026-09-05: real rows from earlier this session predate the
        # "file" field added for finding #60 and crashed this exact call
        # with a missing-argument TypeError). Schema evolution tolerance
        # like this is a real, general pattern — not specific to "file" —
        # so any future field addition should get the same treatment here.
        item.setdefault("file", "<unknown>")
        alerts.append(Alert(**item))
    return alerts


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
        taxonomy_id="PIIE-001", risk_score=9.1,
        plain_english_summary="Raw applicant PII sent to an external LLM without masking.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash/redact PII before building the prompt",
    )
    print("Wrote alert:", alert)
    print("Active alerts:", list_active_alerts())
    resolve_alert(alert.alert_id, approved=True)
    print("Resolved. Active alerts now:", list_active_alerts())
