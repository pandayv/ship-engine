"""
Lambda entrypoint for the public-facing SHIP webhook.

Handles real HTTP requests only (from the Function URL — GitHub's webhook
delivery, dashboard traffic, curl/health checks) via Mangum, wrapping the
existing FastAPI app (src/api/main.py) unchanged.

2026-09-05: this used to also handle an internal async self-invocation
(a marker-based dispatch to process_pr()) so the webhook route could hand
off slow, multi-fragment analysis to a second Lambda invocation of itself.
That design was replaced — see src/api/main.py's module docstring for the
full reasoning — with per-fragment SQS dispatch to a separate Lambda
(fragment_lambda_handler.py, deployed as its own function, ship-fragment-
processor). This handler no longer needs to know about that at all; it's
purely the HTTP-facing entrypoint now.

Deployed with SHIP_DIAGNOSTICIAN_MODE=agentcore so Diagnostician calls the
already-deployed Bedrock AgentCore Runtime remotely instead of building a
Strands Agent in-process — keeps this Lambda's own dependency footprint to
fastapi/mangum/boto3/requests/jinja2 only (see diagnostician.py's lazy
imports), small enough for a plain zip deployment with no container image
and no local Docker needed.
"""

from mangum import Mangum

from src.api.main import app

handler = Mangum(app)
