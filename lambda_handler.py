"""
Lambda entrypoint for the public-facing SHIP webhook.

Handles real HTTP requests only (from the Function URL — GitHub's webhook
delivery, dashboard traffic, curl/health checks) via Mangum, wrapping the
existing FastAPI app (src/api/main.py) unchanged.

Slow, multi-fragment analysis is not handled here. The webhook route dispatches each
escalated fragment to SQS, and a separate Lambda (fragment_lambda_handler.py, deployed as
ship-fragment-processor) processes them. See src/api/main.py's module docstring. This handler
is purely the HTTP-facing entrypoint.

Deployed with SHIP_DETECTOR_MODE=agentcore so Detector calls the
already-deployed Bedrock AgentCore Runtime remotely instead of building a
Strands Agent in-process — keeps this Lambda's own dependency footprint to
fastapi/mangum/boto3/requests/jinja2 only (see detector.py's lazy
imports), small enough for a plain zip deployment with no container image
and no local Docker needed.
"""

from mangum import Mangum

from src.api.main import app

handler = Mangum(app)
