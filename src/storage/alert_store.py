"""
DynamoDB persistence for alerts Triage has frozen — this is what Gate
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
    strings), plus the bookkeeping attributes described below.

What a decision means for the pull request (one rule, used by every reader):
  - approved  the person accepted the risk. The finding stops blocking.
  - rejected  the finding is real. The PR stays blocked (commit status
              failure) until a later commit no longer contains the flagged
              fragment. `fragment_key` identifies the fragment independent of
              the detector verdict; the webhook calls release_fixed_rejections()
              with the keys still present in the PR, and any rejected finding
              whose fragment is gone gets `unblocked_at`. A rejected row written
              before `fragment_key` existed has nothing to compare against, so it
              is NOT held (it was cleared by the old rule and stays cleared).
  A rejected finding is decided, so it never reappears as something to review:
  list_active_alerts() (what a person still has to decide) returns only frozen
  rows, while list_gate_alerts() also returns the rejected rows still holding a
  merge, for the status and the summary.

Delivery bookkeeping: `commented_at` / `notified_at` record that the PR comment
and the Echo notification for a finding went out, each guarded by a short lease
(`comment_claimed_at` / `notify_claimed_at`) so a retry finishes whichever step
is missing without repeating the one already done. See claim_step().

Note on status vs. severity: they answer different questions and both are
needed. `status` is where the alert is in its lifecycle (does it still
need a human?). `severity` is what Triage decided the finding warrants
(does it stop the merge, or is it a flag someone should look at while the
build continues?) — see src/agents/triage.py's three routing bands.
"""

import hashlib
import logging
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache

log = logging.getLogger(__name__)

TABLE_NAME = "ship-alerts"

SEVERITY_BLOCKING = "blocking"  # Triage froze the build; merge should not proceed
SEVERITY_REVIEW = "review"      # flagged for a human, but the build was not stopped

# Groups the six detectors into the four themes "Last line of defense" in
# the README names: a person's data leaving unseen, a decision landing
# unchecked, a protected trait swaying an outcome, a process handed power
# with no limit. Used to cluster findings for review (Gate, the screen
# push) so someone can act on "all the data-exposure ones" as a unit
# instead of reading six unrelated-looking cards. TLGP-001 and TLGP-002
# share a taxonomy prefix but are different themes — grouping by the full
# code, not the prefix, is deliberate.
THEME_ORDER = ["Data exposure", "Human oversight", "Bias & fairness", "Agent capability"]
_THEME_BY_TAXONOMY = {
    "PIIE-001": "Data exposure",
    "PIIE-002": "Data exposure",
    "PIIE-003": "Data exposure",
    "TLGP-002": "Human oversight",
    "ALBP-001": "Bias & fairness",
    "TLGP-001": "Agent capability",
}


def theme_for(taxonomy_id: str) -> str:
    """A fragment that matches more than one detector stores a
    comma-joined taxonomy_id (e.g. "PIIE-001, ALBP-001, TLGP-002"); the
    first one is Detector's primary classification, and is what's used
    for clustering. Unrecognized codes land in "Other" rather than
    raising, new detectors shouldn't break review grouping on day one."""
    primary = taxonomy_id.split(",")[0].strip()
    return _THEME_BY_TAXONOMY.get(primary, "Other")


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
    # The dispositions history needs these two to mean anything — before
    # this, the reason a human gave lived ONLY in the PR comment
    # (comment_for_decision), and the audit record dashboard.py itself
    # calls load-bearing ("the reason IS the audit record") existed nowhere
    # this service could read back. Empty for rows resolved before this
    # field existed.
    resolution_reason: str = ""
    resolved_by: str = ""
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
    # Identifies the flagged code (file + isolated fragment) without the
    # detector's verdict, so a later commit can tell "this fragment is still
    # here" from "it was fixed". Empty for rows written before it existed.
    fragment_key: str = ""
    # When a rejected finding stopped holding the merge because the commit no
    # longer contains its fragment (release_fixed_rejections()).
    unblocked_at: str | None = None
    # Delivery bookkeeping, see claim_step(). The *_claimed_at values are epoch
    # seconds of a lease; 0 means unclaimed.
    commented_at: str | None = None
    notified_at: str | None = None
    comment_claimed_at: int = 0
    notify_claimed_at: int = 0
    # NOT stored. Set by put_alert() on the object it returns: True only when
    # this call created a brand-new row. A re-detection of a finding that is
    # already stored (still frozen, or already resolved by a person) leaves it
    # False. The caller uses it to avoid posting the same finding to the pull
    # request again on every push. See _encode_item(), which drops it.
    newly_written: bool = False


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
    the key: a real retry of the SAME violation re-detects the SAME
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


# Bookkeeping that is absent until it happens. Conditional updates test
# attribute_not_exists() on these, and DynamoDB counts an explicit NULL as
# existing, so an unset value must not be written at all.
_UNSET_UNTIL_SET = ("unblocked_at", "commented_at", "notified_at", "comment_claimed_at", "notify_claimed_at")
_KNOWN_FIELDS = {f.name for f in fields(Alert)}


def _encode_item(alert: Alert) -> dict:
    item = asdict(alert)
    item.pop("newly_written", None)  # a fact about this write, not about the finding
    for name in _UNSET_UNTIL_SET:
        if not item.get(name):
            item.pop(name, None)
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


def fragment_key_for(file: str, fragment_text: str) -> str:
    """Identifies flagged code independent of the detector's verdict (taxonomy,
    score). Two commits that leave a fragment untouched produce the same key."""
    return hashlib.sha256(f"{file}#{fragment_text}".encode()).hexdigest()[:32]


def _is_conditional_failure(exc: Exception) -> bool:
    return type(exc).__name__ == "ConditionalCheckFailedException"


def put_alert(repo: str, pr_number: int, file: str, taxonomy_id: str, fragment_text: str, risk_score: float,
              plain_english_summary: str, citation: str, remediation_patch: str,
              severity: str = SEVERITY_BLOCKING, head_sha: str = "") -> Alert:
    """Records a finding and returns the STORED row.

    A new finding is created with newly_written=True. A finding already on record
    keeps its row (created_at, delivery bookkeeping, and any decision a person made):
    while it is still frozen its verdict fields are refreshed, once decided it is
    left exactly as it is. Either way the caller gets what is stored, so a retry can
    see which delivery steps are still missing.

    severity is deliberately NOT part of _deterministic_alert_id: a re-detection of
    the same violation that scores differently enough to cross a band boundary must
    UPDATE that violation's existing row, not create a second row for the same
    finding at a different severity."""
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
        fragment_key=fragment_key_for(file, fragment_text),
    )
    try:
        # Create only. attribute_not_exists is DynamoDB's own atomic answer to "was
        # a row already here?"; reading first and writing second would race with a
        # concurrent fragment.
        _table().put_item(Item=_encode_item(alert), ConditionExpression="attribute_not_exists(alert_id)")
        alert.newly_written = True
        return alert
    except Exception as e:
        if not _is_conditional_failure(e):
            raise

    # Already on record. Refresh the verdict while a person has not yet decided it;
    # a retry/redelivery for a violation already resolved must NOT re-freeze it or
    # erase their decision.
    try:
        response = _table().update_item(
            Key={"alert_id": alert.alert_id},
            UpdateExpression=(
                "SET risk_score = :score, severity = :severity, plain_english_summary = :summary, "
                "citation = :citation, remediation_patch = :patch, head_sha = :sha, fragment_key = :fkey"
            ),
            ConditionExpression="#s = :frozen",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":score": Decimal(str(risk_score)), ":severity": severity, ":summary": plain_english_summary,
                ":citation": citation, ":patch": remediation_patch, ":sha": head_sha,
                ":fkey": alert.fragment_key, ":frozen": "frozen",
            },
            ReturnValues="ALL_NEW",
        )
        return _row_to_alert(response["Attributes"])
    except Exception as e:
        if not _is_conditional_failure(e):
            raise
    stored = get_alert(alert.alert_id)
    return stored if stored is not None else alert


def _scan_all(**scan_kwargs) -> list[dict]:
    # Every caller feeds a gate decision, the summary Relay speaks, or a screen a
    # person decides from, so the read is strongly consistent: a decision made a
    # moment ago must be in it. (Costs twice the read units of an eventually
    # consistent scan, on a table of tens of rows.)
    scan_kwargs.setdefault("ConsistentRead", True)
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
    # Rows resolved before resolution_reason/resolved_by existed: the
    # reason still went to the PR comment at the time, just not here.
    item.setdefault("resolution_reason", "")
    item.setdefault("resolved_by", "")
    # Attributes a newer writer added are ignored rather than crashing an older reader.
    return Alert(**{k: v for k, v in item.items() if k in _KNOWN_FIELDS})


# --- what blocks a merge: one definition, used by the commit status, the
# summary Relay speaks and the screens ---


def is_open(alert: Alert) -> bool:
    """Still waiting for a person's decision."""
    return alert.status != "resolved"


def is_held(alert: Alert) -> bool:
    """Decided "reject" and the flagged code is still in the PR: the merge stays
    blocked until a commit removes it (see release_fixed_rejections). Not something
    left to review: the person already decided."""
    return (alert.status == "resolved" and alert.resolution == "rejected"
            and bool(alert.fragment_key) and not alert.unblocked_at)


def list_active_alerts() -> list[Alert]:
    """Findings still waiting for a decision (frozen)."""
    items = _scan_all(
        FilterExpression="#s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "frozen"},
    )
    return [_row_to_alert(i) for i in items]


def list_gate_alerts() -> list[Alert]:
    """Everything that can block a merge right now, in one scan: findings waiting
    for a decision plus rejected findings whose code is still in the PR. Split them
    with is_open() / is_held()."""
    items = _scan_all(
        FilterExpression="#s = :frozen OR (#s = :resolved AND #res = :rejected AND attribute_exists(#fk) "
                         "AND attribute_not_exists(#ub))",
        ExpressionAttributeNames={"#s": "status", "#res": "resolution", "#fk": "fragment_key", "#ub": "unblocked_at"},
        ExpressionAttributeValues={":frozen": "frozen", ":resolved": "resolved", ":rejected": "rejected"},
    )
    return [a for a in (_row_to_alert(i) for i in items) if is_open(a) or is_held(a)]


def list_resolved_alerts() -> list[Alert]:
    """
    The Gate's dispositions view: what a human already decided, and why.
    Most-recent-first, same reasoning as repo_store.list_repos() ordering
    a list for a human to scan rather than leaving it in scan order.
    """
    items = _scan_all(
        FilterExpression="#s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": "resolved"},
    )
    alerts = [_row_to_alert(i) for i in items]
    return sorted(alerts, key=lambda a: a.resolved_at or "", reverse=True)


def list_alerts_for_pr(repo: str, pr_number: int) -> list[Alert]:
    """
    Every alert for one PR, resolved ones included (src/api/github_writeback.py's
    derive_commit_status() decides which of them still block).

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
    # Consistent: this feeds decisions (is it still frozen? what did it say?).
    item = _table().get_item(Key={"alert_id": alert_id}, ConsistentRead=True).get("Item")
    return _row_to_alert(item) if item else None


class AlertNotFoundOrAlreadyResolved(Exception):
    """Raised when resolve_alert() targets an alert_id that doesn't exist,
    or that's already been resolved. Review findings #3/#4: DynamoDB's
    update_item upserts by default with no existence check, so a bogus or
    stale alert_id used to silently create a malformed ghost row instead of
    404ing, and an already-resolved alert could be silently re-resolved
    (overwriting a prior rejection with an approval, or vice versa) with no
    conflict detection and no record the first decision ever happened."""


def resolve_alert(alert_id: str, approved: bool, reason: str = "", resolved_by: str = "") -> None:
    try:
        _table().update_item(
            Key={"alert_id": alert_id},
            UpdateExpression=(
                "SET #s = :status, resolution = :resolution, resolved_at = :resolved_at, "
                "resolution_reason = :reason, resolved_by = :resolved_by"
            ),
            ConditionExpression="attribute_exists(alert_id) AND #s = :frozen",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":status": "resolved",
                ":resolution": "approved" if approved else "rejected",
                ":resolved_at": datetime.now(timezone.utc).isoformat(),
                ":reason": reason,
                ":resolved_by": resolved_by,
                ":frozen": "frozen",
            },
        )
    except Exception as e:
        if _is_conditional_failure(e):
            raise AlertNotFoundOrAlreadyResolved(
                f"alert_id {alert_id!r} does not exist or is not currently frozen"
            ) from e
        raise


def release_fixed_rejections(repo: str, pr_number: int, present_fragment_keys: set[str]) -> int:
    """A rejected finding holds the merge until the code changes. Called when a
    commit is processed with the fragment keys the PR still contains: every
    held finding for this PR whose fragment is no longer among them stops
    blocking. Returns how many were released.

    The rule is deliberately about the code, not about time or about the
    detector: a fragment that is still there stays blocked even if a later
    detector run would score it differently, because a person already said it is
    real. A fragment that changed at all gets a new key, is judged again as a new
    finding, and blocks again if it still violates."""
    released = 0
    for alert in list_alerts_for_pr(repo, pr_number):
        if not is_held(alert) or alert.fragment_key in present_fragment_keys:
            continue
        try:
            _table().update_item(
                Key={"alert_id": alert.alert_id},
                UpdateExpression="SET unblocked_at = :now",
                ConditionExpression="#res = :rejected AND attribute_not_exists(unblocked_at)",
                ExpressionAttributeNames={"#res": "resolution"},
                ExpressionAttributeValues={":now": datetime.now(timezone.utc).isoformat(), ":rejected": "rejected"},
            )
            released += 1
        except Exception as e:
            if not _is_conditional_failure(e):
                raise
    return released


# --- delivery bookkeeping: send the comment / the notification once ---

# step -> (attribute that records it was sent, attribute holding the in-flight lease)
_STEPS = {"comment": ("commented_at", "comment_claimed_at"), "notify": ("notified_at", "notify_claimed_at")}
CLAIM_LEASE_SECONDS = 300


def claim_step(alert_id: str, step: str) -> bool:
    """Takes the right to perform one delivery step ("comment" or "notify") for a
    finding. True means the caller should send it and then call complete_step(); False
    means it is already done or another invocation is mid-send.

    A lease, not a permanent claim: if the invocation dies after claiming, the lease
    runs out and a retry sends it. Marking only AFTER a successful send is what makes a
    failed POST or a crash retryable; the lease is what stops two live invocations
    sending it twice. The unlucky window (sent, then died before complete_step) can
    repeat a message once, never lose it."""
    done, claimed = _STEPS[step]
    now = int(time.time())
    try:
        _table().update_item(
            Key={"alert_id": alert_id},
            UpdateExpression=f"SET {claimed} = :now",
            ConditionExpression=(f"attribute_exists(alert_id) AND attribute_not_exists({done}) "
                                 f"AND (attribute_not_exists({claimed}) OR {claimed} < :stale)"),
            ExpressionAttributeValues={":now": now, ":stale": now - CLAIM_LEASE_SECONDS},
        )
        return True
    except Exception as e:
        if not _is_conditional_failure(e):
            log.exception("could not claim the %s step for %s", step, alert_id)
        return False


def complete_step(alert_id: str, step: str) -> None:
    done, claimed = _STEPS[step]
    try:
        _table().update_item(
            Key={"alert_id": alert_id},
            UpdateExpression=f"SET {done} = :now REMOVE {claimed}",
            ExpressionAttributeValues={":now": datetime.now(timezone.utc).isoformat()},
        )
    except Exception:
        log.exception("sent the %s for %s but could not record it: a retry may send it again", step, alert_id)


def release_step(alert_id: str, step: str) -> None:
    """The send failed: give the lease back so the next attempt can try at once."""
    _, claimed = _STEPS[step]
    try:
        _table().update_item(Key={"alert_id": alert_id}, UpdateExpression=f"REMOVE {claimed}")
    except Exception:
        log.exception("could not release the %s lease for %s", step, alert_id)


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
