"""
FastAPI webhook ingestion. Wires Screener -> Detector -> Triage -> alert_store.

A plain FastAPI service, wrapped for Lambda with Mangum (see lambda_handler.py), so one app
serves local runs and the deployed function.

GitHub does not wait minutes for a response, so /api/v1/webhook does the cheap work only
(signature check, payload parse, a whole-diff Screener precheck, the per-fragment scan) and
returns immediately.

Each escalated fragment is its own retryable job. _dispatch_fragments() sends it through SQS
to the fragment-processing Lambda (fragment_lambda_handler.py at the repo root). Fragments of
one PR run concurrently, so a multi-issue PR takes about as long as its slowest fragment, and
no fragment's slow detection starves another. process_pr() is the synchronous, sequential path
used when no queue is configured (SHIP_FRAGMENT_QUEUE_URL unset), so local runs and the test
suite need no AWS.

Rate limits: concurrent fragments mean concurrent Bedrock calls. Calls fire at once, and when
AWS throttles, botocore's `adaptive` retry mode on the shared Bedrock client backs off and
retries. The SQS event source mapping's maximum concurrency caps how many fragments run at the
same time. That keeps throughput near the account's limit without custom coordination.

THE STATUS LIFECYCLE (every accepted commit ends in a real `ship/compliance` status,
because a required check can only be satisfied by one and absence must never pass):

  pending    posted the moment the webhook accepts the commit
  success /  posted by whoever finishes last. Each fragment recomputes the status from
  failure    stored alerts; a green result waits until every queued fragment has
             reported (src/storage/review_ledger.py), a red one is posted at once
  error      a review that could not run: the diff could not be read, the allowlist could
             not be checked, a fragment was too large to queue. Logged at ERROR too

A blocked finding keeps the PR failing until a later commit no longer contains its
flagged code (alert_store.release_fixed_blocks, called from _reconcile()). What is not
covered without new infrastructure: a fragment whose SQS message exhausts its retries
(the check stays pending, which is the safe direction) needs a dead-letter consumer or a
reconciler to turn that into an explicit error status.

Request handling:
- Signature verification fails closed. With GITHUB_WEBHOOK_SECRET unset the request is refused,
  unless SHIP_ALLOW_UNSIGNED=true opts into local-development behavior.
- The local-test `{"code_diff": ...}` payload goes through the same dispatch as a real PR event.
- Only connected repositories are reviewed, so a payload cannot make this service spend its
  GitHub token or Bedrock quota on a repository of the caller's choosing.
- The per-fragment scan happens once, in the fast leg, because it decides what to enqueue.
- The file path is carried into put_alert(), so a multi-file PR's alerts trace back to the
  file that triggered each one.
- Message size is checked against the SQS limit before sending. An oversized fragment is
  logged and reported on the PR, never an unhandled error in the fast leg.
- In process_pr()'s fallback loop, one fragment's exception is recorded and the rest continue.
  In the SQS path each fragment is already an independent job.
"""

import hashlib
import hmac
import json
import logging
import os

import requests
from fastapi import FastAPI, Header, HTTPException, Request

from src.agents.detector import detect
from src.agents.screener import isolate_fragment, scan, split_diff_into_fragments
from src.agents.triage import route
from src.api.dashboard import router as dashboard_router
from src.api.github_client import extract_head_sha, extract_pr_ref, fetch_pr_diff, is_pr_event
from src.api.github_writeback import comment_for_finding, post_commit_status, post_pr_comment, sync_pr_check
from src.api.alexa_notify import notify_new_finding
from src.storage.alert_store import (SEVERITY_BLOCKING, SEVERITY_REVIEW, claim_step, complete_step, fragment_key_for,
                                     put_alert, release_fixed_blocks, release_step)
from src.storage.repo_store import is_watched, mark_event_seen, mark_reviewed, watch_state
from src.storage.review_ledger import begin_review, fragment_reported
from src.storage.status_store import refresh_summary

app = FastAPI(title="SHIP")
app.include_router(dashboard_router)

log = logging.getLogger(__name__)
# Lambda's Python runtime attaches its own handler to the root logger before user code runs,
# which makes logging.basicConfig() a no-op there. Setting the level directly on this logger
# and the root logger works whether or not a handler already exists.
log.setLevel(logging.INFO)
logging.getLogger().setLevel(logging.INFO)

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
ALLOW_UNSIGNED = os.environ.get("SHIP_ALLOW_UNSIGNED", "").lower() == "true"
FRAGMENT_QUEUE_URL = os.environ.get("SHIP_FRAGMENT_QUEUE_URL", "")
SQS_MESSAGE_SIZE_LIMIT = 250_000  # bytes; AWS's real cap is 256KB, small margin for JSON overhead


def _verify_signature(payload_body: bytes, signature_header: str | None) -> None:
    if not GITHUB_WEBHOOK_SECRET:
        if ALLOW_UNSIGNED:
            return  # explicit local-dev opt-out, not the default
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured on the server")
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
    expected = "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _send_comment(alert, repo_full_name: str, pr_number: int) -> None:
    """Posts the finding's PR comment unless it already went out. Marked AFTER a
    successful post, under a short lease (see alert_store.claim_step): a failed POST
    or a crash leaves it unmarked, and the next pass over this finding (an SQS retry,
    a later push that re-detects it) finishes the job without repeating one already done."""
    if getattr(alert, "commented_at", None) or not claim_step(alert.alert_id, "comment"):
        log.info("finding comment already sent or in flight: alert_id=%s", alert.alert_id)
        return
    if post_pr_comment(repo_full_name, pr_number, comment_for_finding(alert)):
        complete_step(alert.alert_id, "comment")
    else:
        release_step(alert.alert_id, "comment")
        log.warning("finding comment not posted, it will be retried on the next pass: alert_id=%s", alert.alert_id)


def _send_notification(alert, summary) -> None:
    """Chimes the person's Echo once per finding, with the same claim/mark discipline
    as the comment. No-one to notify (or a refused send) leaves it retryable."""
    if getattr(alert, "notified_at", None) or not claim_step(alert.alert_id, "notify"):
        return
    if notify_new_finding(alert.alert_id, getattr(summary, "total", 0) or 1):
        complete_step(alert.alert_id, "notify")
    else:
        release_step(alert.alert_id, "notify")


def _review_complete(repo_full_name: str, pr_number: int, head_sha: str, fragment_key: str) -> bool:
    """True when every fragment of this commit's review has reported (see
    src/storage/review_ledger.py). On a datastore error the answer is False: the check
    stays pending rather than turning green on a guess."""
    try:
        return fragment_reported(repo_full_name, pr_number, head_sha, fragment_key)
    except Exception:
        log.exception("could not read the review ledger, leaving the check pending: repo=%s pr=%s",
                      repo_full_name, pr_number)
        return False


def process_fragment(repo_full_name: str, pr_number: int, file: str, isolated_fragment: str,
                     head_sha: str = "", use_ledger: bool = True) -> dict:
    """
    Detect, route, and (if frozen) store the verdict for ONE already-
    escalated, already-isolated code fragment. This is the actual unit of
    work, shared by process_pr()'s sequential local-fallback loop below and the
    SQS-triggered fragment processor (fragment_lambda_handler.py at the repo root), so there is
    one implementation of "what happens to one fragment".

    Deliberately does NOT catch its own exceptions — raises on failure.
    The two callers need different policies for that: process_pr()'s loop
    catches per-fragment so one failure doesn't abort the rest of the same
    sequential run; the SQS handler wants the exception to
    propagate so SQS's own retry/DLQ mechanism handles it instead.

    EVERY outcome reports to the pull request, not only the ones that create a
    finding. The webhook posts "pending" when it accepts a commit, so a fragment the
    detector clears (no match, dismissed, below the review floor) must still
    contribute to the final status; otherwise a clean commit stays pending forever.
    `use_ledger=False` is the sequential local path, which has no ledger.
    """
    verdict = detect(isolated_fragment)  # requires AWS credentials — will raise if not configured

    # route() takes the DetectorOutput directly.
    decision = route(verdict)

    alert_id = None
    alert = None
    if decision.creates_alert:
        # Both blocking (FREEZE) and non-blocking (REVIEW) findings create
        # a Gate record — that middle band exists precisely so a
        # confirmed-but-not-severe finding reaches a human instead of
        # disappearing into a log. Only the severity recorded on the alert
        # differs. Below-review-floor and dismissed-false-positive cases
        # are still not persisted.
        # The alert_id is derived from (repo, pr_number, file, taxonomy_id)
        # and the isolated fragment text (exactly what Detector judged), so a
        # real retry of the same violation re-derives the same id and finds
        # its own row, which makes SQS's at-least-once delivery safe.
        alert = put_alert(
            repo=repo_full_name, pr_number=pr_number, file=file,
            taxonomy_id=verdict.taxonomy_id, fragment_text=isolated_fragment, risk_score=verdict.risk_score,
            plain_english_summary=verdict.plain_english_summary,
            citation=verdict.citation, remediation_patch=verdict.remediation_patch or "",
            severity=SEVERITY_BLOCKING if decision.blocks_build else SEVERITY_REVIEW,
            head_sha=head_sha,
        )  # requires dynamodb:* permissions
        alert_id = alert.alert_id

        # Put the finding where the work happens. A finding a person has already
        # decided gets no new comment ("merge blocked" would contradict them). An
        # open one gets its comment unless it already went out; see _send_comment.
        if getattr(alert, "status", "frozen") != "resolved":
            _send_comment(alert, repo_full_name, pr_number)
        else:
            log.info("finding already decided, not commenting again: alert_id=%s", alert.alert_id)

    # Recompute the PR's commit status from every stored alert. Fails soft by design
    # (see src/api/github_writeback.py's FAILURE POLICY): the alert is already stored,
    # and raising would send this message back through SQS for a retry that re-runs the
    # model call purely to redeliver a status.
    final = (_review_complete(repo_full_name, pr_number, head_sha, fragment_key_for(file, isolated_fragment))
             if use_ledger and repo_full_name != "local-test" else True)
    sync_pr_check(repo_full_name, pr_number, head_sha, final=final)

    if alert is not None:
        # Rebuild the summary Relay reads. Done here, on the write side,
        # because this invocation has a 900s budget while Relay has 500ms
        # (see src/storage/status_store.py). Best-effort: the alert is
        # already stored, and the next finding or decision re-refreshes.
        summary = refresh_summary()

        # Tell the person on their Echo, once per finding, and only while it is still
        # waiting for them. Best effort, like the comment and the check.
        if getattr(alert, "status", "frozen") != "resolved":
            _send_notification(alert, summary)

    mark_reviewed(repo_full_name)
    log.info("fragment result: file=%s action=%s blocks_build=%s alert_id=%s",
             file, decision.action.value, decision.blocks_build, alert_id)
    return {
        "file": file,
        "action": decision.action.value,
        "blocks_build": decision.blocks_build,
        "reason": decision.reason,
        "detector": verdict.model_dump(),
        "alert_id": alert_id,
    }


def _escalate(code_diff: str) -> tuple[int, list[dict]]:
    """Splits the diff and keeps the fragments Screener flags, each with the key that
    identifies its code. Pure and cheap (regex only), so the webhook can do it before
    anything is queued."""
    fragments = split_diff_into_fragments(code_diff)
    escalated = []
    for frag in fragments:
        screener_result = scan(frag["text"])
        if not screener_result.matched:
            continue
        isolated = isolate_fragment(frag["text"], screener_result.matched_terms)
        escalated.append({"file": frag["file"], "isolated_fragment": isolated,
                          "fragment_key": fragment_key_for(frag["file"], isolated)})
    return len(fragments), escalated


def _reconcile(repo_full_name: str, pr_number: int, escalated: list[dict]) -> None:
    """A blocked finding keeps the PR blocked until its code is gone. This commit's
    flagged fragments are the evidence: any held finding whose fragment is not among
    them is released (alert_store.release_fixed_blocks). Fails closed: if this
    cannot run, the blocked findings simply keep blocking."""
    if repo_full_name == "local-test":
        return
    try:
        released = release_fixed_blocks(repo_full_name, pr_number, {e["fragment_key"] for e in escalated})
        if released:
            log.info("released %d blocked finding(s) whose code is gone: repo=%s pr=%s",
                     released, repo_full_name, pr_number)
            refresh_summary()
    except Exception:
        log.exception("could not release blocked findings, they keep blocking: repo=%s pr=%s",
                      repo_full_name, pr_number)


def process_pr(repo_full_name: str, pr_number: int, code_diff: str, head_sha: str = "") -> dict:
    """
    Sequential local-fallback path: split into fragments, scan each, and
    call process_fragment() for every escalated one, one after another in
    this single process. Used only when SHIP_FRAGMENT_QUEUE_URL isn't
    configured (local dev / tests) — the real deployed path is
    _dispatch_fragments() below, which sends each escalated fragment to
    SQS as its own independent job instead of looping through them all
    here. A PR with enough escalated fragments can exceed a single Lambda
    invocation's hard 900-second timeout partway through and silently lose the
    fragments that never got their turn.
    """
    fragment_count, escalated = _escalate(code_diff)
    results = []
    log.info("process_pr start: repo=%s pr=%s fragments=%d", repo_full_name, pr_number, fragment_count)
    _reconcile(repo_full_name, pr_number, escalated)

    for frag in escalated:
        try:
            results.append(process_fragment(repo_full_name, pr_number, frag["file"], frag["isolated_fragment"],
                                            head_sha, use_ledger=False))
        except Exception as e:
            # A single fragment's failure (a transient AWS hiccup, a schema-validation error
            # from a malformed model response) must not abort the fragments after it. Record
            # the failure and keep going: a partial result is better than a silent gap.
            log.exception("fragment failed: file=%s repo=%s pr=%s", frag["file"], repo_full_name, pr_number)
            results.append({
                "file": frag["file"],
                "action": "error",
                "reason": f"{type(e).__name__}: {e}",
                "detector": None,
                "alert_id": None,
            })

    if not escalated:
        sync_pr_check(repo_full_name, pr_number, head_sha)
    elif any(r["action"] == "error" for r in results):
        # A fragment that could not be judged must not leave a green check behind it.
        post_commit_status(repo_full_name, pr_number, "error", "SHIP could not review part of this commit", head_sha)

    # Derived from `results` rather than kept as separate mutable state.
    any_frozen = any(r["action"] == "freeze" for r in results)
    log.info("process_pr done: repo=%s pr=%s any_frozen=%s escalated=%d", repo_full_name, pr_number, any_frozen, len(results))
    return {
        "action": "freeze" if any_frozen else "pass",
        "fragments_scanned": fragment_count,
        "fragments_escalated": len(results),
        "results": results,
    }


def _dispatch_fragments(repo_full_name: str, pr_number: int, code_diff: str, head_sha: str = "") -> dict:
    """
    Splits the diff, screens each fragment, and sends one SQS message per
    escalated fragment to the fragment-processing queue — each fragment
    becomes its own independent, retryable job instead of being processed
    sequentially inside one Lambda invocation bounded by Lambda's hard
    900-second ceiling.

    Falls back to process_pr()'s synchronous loop if no queue is
    configured (SHIP_FRAGMENT_QUEUE_URL unset), so local dev and the test
    suite need no AWS.

    Anything that could not be queued is reported on the PR as an error status and
    logged at ERROR, never passed: a fragment SHIP did not review is not a fragment
    SHIP cleared.
    """
    if not FRAGMENT_QUEUE_URL:
        return process_pr(repo_full_name, pr_number, code_diff, head_sha)

    _, escalated = _escalate(code_diff)
    _reconcile(repo_full_name, pr_number, escalated)

    if not escalated:
        sync_pr_check(repo_full_name, pr_number, head_sha)
        return {"action": "pass", "reason": "Screener found no trigger matches on a per-fragment scan."}

    # Everything the review must hear back from, including fragments too large to queue:
    # those never report, so the check cannot turn green on a commit SHIP only half reviewed.
    begin_review(repo_full_name, pr_number, head_sha, [e["fragment_key"] for e in escalated])

    import boto3

    client = boto3.client("sqs")
    sent = 0
    skipped = []
    try:
        for frag in escalated:
            payload_bytes = json.dumps({
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "file": frag["file"],
                "isolated_fragment": frag["isolated_fragment"],
                "head_sha": head_sha,
            }).encode("utf-8")

            # SQS's message-size cap is 256KB. An uncaught ClientError here would surface as an
            # unhandled 500 in the fast leg.
            if len(payload_bytes) > SQS_MESSAGE_SIZE_LIMIT:
                log.error("fragment too large to enqueue, NOT reviewed: file=%s bytes=%d repo=%s pr=%s",
                          frag["file"], len(payload_bytes), repo_full_name, pr_number)
                skipped.append(frag["file"])
                continue

            client.send_message(QueueUrl=FRAGMENT_QUEUE_URL, MessageBody=payload_bytes.decode("utf-8"))
            sent += 1
    except Exception as e:
        log.exception("could not queue fragments: repo=%s pr=%s", repo_full_name, pr_number)
        post_commit_status(repo_full_name, pr_number, "error", "SHIP could not queue this commit for review",
                           head_sha, trust_head=True)
        raise HTTPException(status_code=502, detail=f"Could not queue the review: {type(e).__name__}") from e

    if skipped:
        noun = "fragment" if len(skipped) == 1 else "fragments"
        post_commit_status(repo_full_name, pr_number, "error",
                           f"SHIP could not review {len(skipped)} {noun}: too large to queue", head_sha, trust_head=True)
        if sent == 0:
            return {"action": "error", "reason": "Every escalated fragment was too large to enqueue — none analyzed."}
    return {"action": "accepted", "reason": f"Screener flagged {sent} fragment(s); each queued for independent review."
            + (f" {len(skipped)} too large to queue." if skipped else "")}


@app.post("/api/v1/webhook")
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(default=None)):
    body = await request.body()
    _verify_signature(body, x_hub_signature_256)
    payload = await request.json()

    if is_pr_event(payload):
        repo_full_name, pr_number = extract_pr_ref(payload)
        head_sha = extract_head_sha(payload)
        # Don't let a payload name an arbitrary repo and spend this service's GitHub token or
        # Bedrock quota against it. A point lookup against the connected-repositories table, so
        # connecting a repo is a dashboard action and the check stays constant-time as the list
        # grows. Fails closed on a datastore error (see src/storage/repo_store.py).
        if not is_watched(repo_full_name):
            if watch_state(repo_full_name) == "unknown":
                # The datastore could not say whether this repository is connected. Refusing
                # is right, but a required check must not be left to pass by absence.
                log.error("cannot tell whether %s is connected, review not run: pr=%s", repo_full_name, pr_number)
                post_commit_status(repo_full_name, pr_number, "error",
                                   "SHIP could not verify this repository, so the review did not run",
                                   head_sha, trust_head=True)
                raise HTTPException(status_code=503, detail="Could not verify the repository allowlist")
            raise HTTPException(
                status_code=403,
                detail=f"Repo {repo_full_name!r} is not connected to SHIP",
            )
        # Best-effort: flips the repo from "awaiting first event" to
        # "watching" in the dashboard. Never allowed to fail the delivery.
        mark_event_seen(repo_full_name)
        # A required check can only be satisfied by a status. Say "reviewing" the moment
        # the commit is accepted, so absence is never mistaken for a pass; every path
        # below ends in a real status.
        post_commit_status(repo_full_name, pr_number, "pending", "SHIP is reviewing this commit",
                           head_sha, trust_head=True)
        # A closed or deleted PR, an expired token or a transient GitHub 5xx raises here. Log it
        # clearly and return a real HTTP error that GitHub's webhook redelivery can react to.
        try:
            code_diff = fetch_pr_diff(repo_full_name, pr_number)
        except requests.exceptions.RequestException as e:
            log.exception("fetch_pr_diff failed: repo=%s pr=%s", repo_full_name, pr_number)
            post_commit_status(repo_full_name, pr_number, "error", "SHIP could not read this commit's diff",
                               head_sha, trust_head=True)
            raise HTTPException(status_code=502, detail=f"Could not fetch PR diff from GitHub: {e}") from e
    else:
        # Simplified {"code_diff": "..."} body, for local testing without a
        # real GitHub webhook payload.
        repo_full_name, pr_number = "local-test", 0
        code_diff = payload.get("code_diff", "")
        head_sha = ""

    if not code_diff:
        _reconcile(repo_full_name, pr_number, [])
        sync_pr_check(repo_full_name, pr_number, head_sha)
        return {"action": "pass", "reason": "No diff content in the payload."}

    # Cheap whole-diff precheck (pure regex, no network) — if literally
    # nothing matches anywhere, skip fragment dispatch entirely rather than
    # spending the work to split and per-fragment-scan for nothing.
    if not scan(code_diff).matched:
        # Report the pass on the PR too, replacing the pending status: a required check
        # that never reports blocks the merge just as effectively as a failing one. Any
        # blocked finding whose code is gone is released first.
        _reconcile(repo_full_name, pr_number, [])
        sync_pr_check(repo_full_name, pr_number, head_sha)
        mark_reviewed(repo_full_name)
        return {"action": "pass", "reason": "Screener found no trigger matches."}

    # Both payload shapes go through the same dispatch; _dispatch_fragments() falls back to the
    # sequential process_pr() path only when no fragment queue is configured at all.
    return _dispatch_fragments(repo_full_name, pr_number, code_diff, head_sha)
