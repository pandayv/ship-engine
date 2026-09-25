"""
The pre-computed release summary — the single item Herald reads.

WHY THIS EXISTS. Alexa+ allows a round trip of under 500 milliseconds, and
that budget is spent before a person hears anything. Aggregating findings
on demand fails the wrong way: it is fast today only because there are few
findings, and it gets slower precisely as SHIP becomes more useful. So the
aggregation moves to the write side, where the budget actually exists — a
fragment processor has a 900-second ceiling and can afford to recompute;
Herald has half a second and can only afford one GetItem.

RECOMPUTED, NOT INCREMENTED. Every writer rebuilds the whole summary from
the alerts table rather than adjusting a counter. That costs more per
write and buys two things worth more than the saving:

  - It is race-safe without coordination. Fragments are processed in
    parallel, so several writers can refresh this item at once. A counter
    would need every increment to land exactly once; a full recomputation
    is correct whoever writes it last, and a write racing with a slightly
    older one is corrected by the next refresh rather than drifting
    permanently. This is the same reasoning as sync_pr_check() in
    src/api/github_writeback.py.
  - It cannot silently diverge from the truth. A counter that misses one
    decrement stays wrong forever, and the failure is invisible: the
    number simply reads wrong to whoever asks. A recomputation is
    self-healing by construction.

WHAT VOICE IS ALLOWED TO KNOW. This summary deliberately holds counts,
pull-request labels and timestamps — not citations, file paths, taxonomy
ids or code. That is not an accident of what was convenient to store: it
is the modality contract expressed in the data model, so the fast path
voice reads from does not even contain the detail voice must never speak.
Screen surfaces read the alerts themselves.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-status"

# One well-known row. The summary is global by design: the question voice
# asks ("is anything blocking a release") spans every connected repository,
# and a person with two repos does not want to ask twice.
SUMMARY_KEY = "release-summary"


@dataclass
class PullRequestCounts:
    repo: str
    pr_number: int
    blocking: int
    review: int
    oldest_finding_at: str


@dataclass
class ReleaseSummary:
    blocking: int = 0
    review: int = 0
    pull_requests: list[PullRequestCounts] = field(default_factory=list)
    computed_at: str = ""

    @property
    def total(self) -> int:
        return self.blocking + self.review


@lru_cache(maxsize=1)
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(TABLE_NAME)


def create_table_if_not_exists() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    try:
        client.describe_table(TableName=TABLE_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "summary_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "summary_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def compute_summary() -> ReleaseSummary:
    """Rebuilds the summary from the alerts table. Called on the WRITE path
    only — never by Herald, which must not pay this cost."""
    from src.storage.alert_store import SEVERITY_BLOCKING, list_active_alerts

    buckets: dict[tuple[str, int], list] = {}
    for alert in list_active_alerts():
        buckets.setdefault((alert.repo, alert.pr_number), []).append(alert)

    counts = [
        PullRequestCounts(
            repo=repo,
            pr_number=pr,
            blocking=sum(1 for a in items if a.severity == SEVERITY_BLOCKING),
            review=sum(1 for a in items if a.severity != SEVERITY_BLOCKING),
            oldest_finding_at=min(a.created_at for a in items),
        )
        for (repo, pr), items in buckets.items()
    ]
    counts.sort(key=lambda c: c.oldest_finding_at)

    return ReleaseSummary(
        blocking=sum(c.blocking for c in counts),
        review=sum(c.review for c in counts),
        pull_requests=counts,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


def refresh_summary() -> ReleaseSummary:
    """Recompute and store. Best-effort by design: a failure here must never
    fail the finding that triggered it, because the alert itself is already
    durably stored and the next refresh will correct this one."""
    summary = compute_summary()
    try:
        item = {"summary_id": SUMMARY_KEY, **asdict(summary)}
        _table().put_item(Item=item)
    except Exception:
        log.exception("could not refresh the release summary — Herald may serve a stale count "
                      "until the next finding or decision triggers another refresh")
    return summary


def read_summary() -> ReleaseSummary:
    """Herald's entire read path: one GetItem, no aggregation, no scan.

    An empty or unreadable summary reports "nothing blocked" rather than
    raising. That is the honest degradation for a voice surface — the
    alternative is an assistant that errors at someone instead of
    answering, and the authoritative state is always the dashboard.
    """
    try:
        item = _table().get_item(Key={"summary_id": SUMMARY_KEY}).get("Item")
    except Exception:
        log.exception("could not read the release summary")
        return ReleaseSummary()

    if not item:
        return ReleaseSummary()

    return ReleaseSummary(
        blocking=int(item.get("blocking", 0)),
        review=int(item.get("review", 0)),
        pull_requests=[
            PullRequestCounts(
                repo=p["repo"], pr_number=int(p["pr_number"]),
                blocking=int(p.get("blocking", 0)), review=int(p.get("review", 0)),
                oldest_finding_at=p.get("oldest_finding_at", ""),
            )
            for p in item.get("pull_requests", [])
        ],
        computed_at=item.get("computed_at", ""),
    )
