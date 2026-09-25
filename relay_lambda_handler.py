"""
Lambda entrypoint for ship-relay, SHIP's MCP server.

Deliberately does NOT import the module-level `app`/`mcp` objects from
src/relay/server.py. It calls create_app() to build a fresh ASGI app and
a fresh Mangum wrapper on every single invocation.

This is not caution for its own sake — it is required. See
src/relay/server.py's ARCHITECTURE NOTE for the full reasoning: the MCP
SDK's Streamable HTTP session manager can only run its lifespan once per
instance, and a warm Lambda container reusing a module-level app object
would crash on the second request. A fresh app per invocation has a
never-run session manager every time, which satisfies that constraint by
construction rather than working around it. Verified against the real
failure mode (multiple independent invocations, not a single call) before
this design was relied on.

The cost of rebuilding per invocation is small and measured, not assumed:
~2ms to construct and register five tools on a fresh FastMCP instance,
against a 500ms total budget.
"""

from mangum import Mangum

from src.relay.server import create_app


def handler(event, context):
    app = create_app()
    return Mangum(app)(event, context)
