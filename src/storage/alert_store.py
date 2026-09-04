"""
DynamoDB persistence for alerts Triage has frozen — this is what Attending
reads from. NOT LIVE-TESTED as of this writing: our IAM user (ship-agent)
doesn't have dynamodb:* permissions yet. Code is written to the real boto3
DynamoDB API, ready to run once that's added — nothing here is mocked.

Table schema (single table, on-demand billing to avoid provisioning
decisions during a hackathon sprint):
  - Partition key: alert_id (string) — a UUID generated at write time
  - Attributes: repo, pr_number, taxonomy_id, risk_score,
    plain_english_summary, citation, remediation_patch, status
    ("frozen" | "resolved"), resolution ("approved" | "rejected" | None),
    created_at, resolved_at (ISO 8601 strings)
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal

TABLE_NAME = "ship-alerts"


@dataclass
class Alert:
    alert_id: str
    repo: str
    pr_number: int
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


def put_alert(repo: str, pr_number: int, taxonomy_id: str, risk_score: float,
              plain_english_summary: str, citation: str, remediation_patch: str) -> Alert:
    alert = Alert(
        alert_id=str(uuid.uuid4()),
        repo=repo,
        pr_number=pr_number,
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
    _table().put_item(Item=item)
    return alert


def list_active_alerts() -> list[Alert]:
    response = _table().scan(FilterExpression="#s = :status", ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":status": "frozen"})
    alerts = []
    for item in response.get("Items", []):
        # DynamoDB's Number type has no int/float distinction — everything
        # numeric comes back as Decimal; convert back to what the rest of
        # the app (and JSON serialization) actually expects
        item["risk_score"] = float(item["risk_score"])
        item["pr_number"] = int(item["pr_number"])
        alerts.append(Alert(**item))
    return alerts


def resolve_alert(alert_id: str, approved: bool) -> None:
    _table().update_item(
        Key={"alert_id": alert_id},
        UpdateExpression="SET #s = :status, resolution = :resolution, resolved_at = :resolved_at",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "resolved",
            ":resolution": "approved" if approved else "rejected",
            ":resolved_at": datetime.now(timezone.utc).isoformat(),
        },
    )


if __name__ == "__main__":
    # will only run once dynamodb:* permissions are added to ship-agent's policy
    create_table_if_not_exists()
    alert = put_alert(
        repo="pandayv/micro-finance", pr_number=1, taxonomy_id="PIIE-001", risk_score=9.1,
        plain_english_summary="Raw applicant PII sent to an external LLM without masking.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash/redact PII before building the prompt",
    )
    print("Wrote alert:", alert)
    print("Active alerts:", list_active_alerts())
    resolve_alert(alert.alert_id, approved=True)
    print("Resolved. Active alerts now:", list_active_alerts())
