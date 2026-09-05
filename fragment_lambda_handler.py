"""
Lambda entrypoint for the SQS-triggered fragment processor (ship-fragment-
processor). Deployed 2026-09-05 to replace the old single-Lambda,
sequential-per-PR design — see src/api/main.py's module docstring for the
full reasoning: a live end-to-end test proved that design could silently
fail to finish reviewing a PR with 3+ escalated fragments, killed mid-run
by AWS Lambda's hard, non-negotiable 900-second function timeout.

Each SQS message is exactly one already-escalated, already-isolated code
fragment (queued by src/api/main.py's _dispatch_fragments()). This
function's only job is to diagnose, route, and (if frozen) store it via
process_fragment() — the same function process_pr()'s local-fallback loop
uses, so there is exactly one implementation of "what happens to one
fragment," not two.

Deployed with BatchSize=1 on the SQS-Lambda event source mapping, so this
handler always processes exactly one message per invocation — no partial-
batch-failure handling needed. If process_fragment() raises (a genuine
failure, not something already handled internally), this handler lets the
exception propagate: Lambda reports the invocation as failed, SQS makes
the message visible again for retry, and after the queue's configured
maxReceiveCount, the message moves to the dead-letter queue instead of
being retried forever or silently dropped — the DLQ + backlog visibility
the original architecture review (finding #40) flagged as missing.

Deployed with SHIP_DIAGNOSTICIAN_MODE=agentcore, same as ship-webhook.
"""

import json

from src.api.main import process_fragment


def handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])
        process_fragment(
            repo_full_name=body["repo_full_name"],
            pr_number=body["pr_number"],
            file=body["file"],
            isolated_fragment=body["isolated_fragment"],
        )
