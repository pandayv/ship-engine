"""
Lambda entrypoint for the SQS-triggered fragment processor (ship-fragment-processor).

Each SQS message is exactly one already-escalated, already-isolated code fragment, queued by
src/api/main.py's _dispatch_fragments(). Reviewing fragments as separate jobs keeps a PR with
many findings clear of Lambda's 900-second limit on a single invocation (see that module's
docstring). This function's only job is to detect, route, and (if frozen) store the fragment
via process_fragment(), the same function process_pr()'s local-fallback loop uses, so there is
one implementation of "what happens to one fragment".

Deployed with BatchSize=1 on the SQS event source mapping, so each invocation handles exactly
one message and no partial-batch-failure handling is needed. If process_fragment() raises (a
real failure, not something already handled internally), this handler lets the exception
propagate: Lambda reports the invocation as failed, SQS makes the message visible again for
retry, and after the queue's maxReceiveCount the message moves to the dead-letter queue
instead of being retried forever or silently dropped.

Deployed with SHIP_DETECTOR_MODE=agentcore, same as ship-webhook.
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
            # .get() rather than [] deliberately: head_sha was added to the
            # message shape after this queue was already live, so a message
            # enqueued by the previous version must still process rather
            # than KeyError its way into the dead-letter queue. An empty
            # sha degrades to "no commit status posted", not a failure.
            head_sha=body.get("head_sha", ""),
        )
