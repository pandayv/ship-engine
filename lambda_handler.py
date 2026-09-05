"""
Lambda entrypoint for the public-facing SHIP webhook.

Handles two distinct event shapes:
1. A real HTTP request (from the Function URL, e.g. GitHub's webhook
   delivery, or curl/health checks) — passed through to the existing
   FastAPI app (src/api/main.py) via Mangum, unchanged.
2. An internal async self-invocation (src/api/main.py's
   _invoke_async_processing) carrying the ASYNC_MARKER key — routed
   directly to process_pr(), bypassing Mangum/FastAPI/HTTP entirely. This
   is what lets the webhook route respond to GitHub immediately while the
   actual (multi-minute, several-Bedrock-calls) analysis happens in this
   second, separate invocation. See src/api/main.py's module docstring for
   why this split exists.

Deployed with SHIP_DIAGNOSTICIAN_MODE=agentcore so Diagnostician calls the
already-deployed Bedrock AgentCore Runtime remotely instead of building a
Strands Agent in-process — keeps this Lambda's own dependency footprint to
fastapi/mangum/boto3/requests/jinja2 only (see diagnostician.py's lazy
imports), small enough for a plain zip deployment with no container image
and no local Docker needed.
"""

from mangum import Mangum

from src.api.main import ASYNC_MARKER, app, process_pr

_mangum_handler = Mangum(app)


def handler(event, context):
    if isinstance(event, dict) and event.get(ASYNC_MARKER) == "process_pr":
        return process_pr(event["repo_full_name"], event["pr_number"], event["code_diff"])
    return _mangum_handler(event, context)
