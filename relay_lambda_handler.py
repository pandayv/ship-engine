"""
Lambda entrypoint for ship-relay, SHIP's MCP server.

Deliberately does NOT import the module-level `app`/`mcp` objects from
src/relay/server.py. It calls create_app() to build a fresh ASGI app and
a fresh Mangum wrapper on every single invocation.

This is required. See src/relay/server.py's ARCHITECTURE NOTE for the full reasoning: the MCP
SDK's Streamable HTTP session manager can only run its lifespan once per instance, and a warm
Lambda container reusing a module-level app object would crash on the second request. A fresh
app per invocation has a never-run session manager every time, which satisfies that
constraint by construction.

The cost of rebuilding per invocation is small: ~2ms to construct and register the tools on a fresh FastMCP instance,
against a 500ms total budget.
"""

import logging

from mangum import Mangum

from src.relay.audit import set_request_id
from src.relay.server import create_app

# Lambda's runtime attaches its handler to the root logger at WARNING, so without this
# Relay's own info lines (the audit trail) never reach CloudWatch.
logging.getLogger().setLevel(logging.INFO)


def handler(event, context):
    set_request_id(getattr(context, "aws_request_id", ""))
    app = create_app()
    return Mangum(app)(event, context)
