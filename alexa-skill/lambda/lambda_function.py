"""
SHIP Review: the Alexa skill that stands in for an Alexa+ add-on.

Alexa+ add-ons are not open to hackathon participants, so a custom skill
carries the same conversation: Alexa hears the person and works out which
intent they meant, and this function turns that intent into a call to
Relay, SHIP's MCP server. Relay decides everything that matters (what to
say, what a voice may do, where a finding is shown). This file only
translates, so voice still goes through the MCP server.

NO DEPENDENCIES ON PURPOSE. The ASK CLI zips this folder as-is, so it uses
only the standard library and boto3, which the Lambda runtime provides.

NO SIGNATURE CHECK ON PURPOSE. This function is invoked through an Alexa
Skills Kit trigger that carries the skill ID. Amazon's documentation says
that trigger already rejects any caller that is not this skill, so there is
nothing to verify in code.

THE 8 SECOND RULE. Alexa gives a skill 8 seconds to answer. Every call to
Relay below has a shorter timeout, and a failure becomes a spoken apology,
never an exception.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

RELAY_URL = os.environ.get(
    "SHIP_RELAY_URL",
    "https://4f5g7mxyhhhhctmbagr5syfloa0airkl.lambda-url.us-west-2.on.aws/mcp",
)
USERS_TABLE = os.environ.get("SHIP_ALEXA_USERS_TABLE", "ship-alexa-users")
PROTOCOL_VERSION = "2025-11-25"
RELAY_TIMEOUT_SECONDS = 5
# Alexa abandons the skill at 8 seconds. Stop waiting a little before that so
# the person hears our apology instead of Alexa's generic failure message.
ANSWER_BY_SECONDS = 7.0

REPROMPT = "You can ask what's up, or say show it on the TV."

# One MCP session per warm container. Relay is stateless, so this is the
# handshake a client is expected to perform, done once instead of per request.
_mcp = {"ready": False, "protocol": None, "tools": [], "next_id": 0}
_table_handle = None
_deadline = float("inf")   # set at the start of every request by handler()


class RelayError(Exception):
    """Relay could not be reached or answered with an error."""


# ---------------------------------------------------------------- MCP client

def _post(body, protocol=None):
    """POST one JSON-RPC message. Returns the parsed reply, or None for a notification."""
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if protocol:
        headers["mcp-protocol-version"] = protocol
    request = urllib.request.Request(
        RELAY_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    remaining = _deadline - time.time()
    if remaining < 0.5:
        raise RelayError("out of time before Alexa's limit")
    try:
        with urllib.request.urlopen(request, timeout=min(RELAY_TIMEOUT_SECONDS, remaining)) as response:
            raw = response.read().decode("utf-8")
            content_type = response.headers.get("content-type", "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RelayError(f"could not reach Relay: {exc}") from exc

    if not raw.strip():
        return None
    if "text/event-stream" in content_type:
        last = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                last = line[5:].strip()
        return json.loads(last) if last else None
    return json.loads(raw)


def _rpc(method, params=None, protocol=None):
    _mcp["next_id"] += 1
    reply = _post(
        {"jsonrpc": "2.0", "id": _mcp["next_id"], "method": method, "params": params or {}},
        protocol,
    )
    if not reply:
        raise RelayError(f"empty reply to {method}")
    if reply.get("error"):
        raise RelayError(reply["error"].get("message", f"error in {method}"))
    return reply["result"]


def _connect():
    """initialize, then the initialized notification, then tools/list. Once per container."""
    if _mcp["ready"]:
        return
    init = _rpc(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ship-alexa-skill", "version": "1.0"},
        },
    )
    protocol = init["protocolVersion"]
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, protocol)
    listed = _rpc("tools/list", protocol=protocol)
    _mcp["protocol"] = protocol
    _mcp["tools"] = [tool["name"] for tool in listed.get("tools", [])]
    _mcp["ready"] = True


def call_tool(name, arguments=None):
    """Returns the tool's reply: parsed JSON for dict tools, plain text for string tools."""
    _connect()
    if name not in _mcp["tools"]:
        raise RelayError(f"Relay does not list a tool named {name}")
    result = _rpc("tools/call", {"name": name, "arguments": arguments or {}}, _mcp["protocol"])
    content = result.get("content") or [{}]
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


# ------------------------------------------------------------ remembering who

def _users_table():
    global _table_handle
    if _table_handle is None:
        import boto3

        _table_handle = boto3.resource("dynamodb").Table(USERS_TABLE)
    return _table_handle


def _short(user_id):
    return (user_id[:18] + "..." + user_id[-6:]) if user_id and len(user_id) > 28 else user_id


def remember_user(user_id, source):
    """Saves the Alexa user ID so SHIP can send this person a notification later.
    Never allowed to break the spoken answer."""
    if not user_id:
        return
    try:
        _users_table().put_item(
            Item={"user_id": user_id, "last_seen": int(time.time()), "source": source}
        )
    except Exception:  # noqa: BLE001 - the voice path must not fail because of bookkeeping
        log.exception("could not save user id")


# --------------------------------------------------------------- Alexa output

def speak(text, end=False, reprompt=None):
    response = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if reprompt and not end:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "sessionAttributes": {}, "response": response}


def _slot_value(slots, name):
    """The canonical id Alexa matched for a custom-type slot, if it matched one."""
    slot = (slots or {}).get(name) or {}
    try:
        authority = slot["resolutions"]["resolutionsPerAuthority"][0]
        if authority["status"]["code"] == "ER_SUCCESS_MATCH":
            return authority["values"][0]["value"]["id"]
    except (KeyError, IndexError, TypeError):
        pass
    return None


# ------------------------------------------------------------------- intents

def status_reply():
    sentence = call_tool("release_status")
    return speak(str(sentence), reprompt=REPROMPT)


def show_reply(slots):
    screen = _slot_value(slots, "screen")
    if not screen:
        return speak("Which screen? The TV, the iPad, or the laptop?", reprompt="Which screen?")
    result = call_tool("display_on", {"surface": screen})
    return speak(result.get("spoken_response", "Okay."), reprompt=REPROMPT)


def on_intent(request):
    name = request["intent"]["name"]
    slots = request["intent"].get("slots")
    if name == "StatusIntent":
        return status_reply()
    if name == "ShowIntent":
        return show_reply(slots)
    if name == "AMAZON.HelpIntent":
        return speak(
            "I tell you whether anything is blocked, and I send the detail to a screen. "
            "Ask what's up, or say show it on the TV.",
            reprompt=REPROMPT,
        )
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
        return speak("Okay.", end=True)
    return speak("I didn't catch that. " + REPROMPT, reprompt=REPROMPT)


# --------------------------------------------------------------------- entry

def handler(event, context):
    global _deadline
    _deadline = time.time() + ANSWER_BY_SECONDS
    request = event.get("request", {})
    kind = request.get("type", "")
    user_id = (event.get("context", {}).get("System", {}).get("user", {}) or {}).get("userId")

    # Skill events carry the user ID in the request body; ordinary requests carry
    # it in the context. Log both forms so the two can be compared on a real device.
    if kind.startswith("AlexaSkillEvent."):
        event_user = request.get("userId") or user_id
        log.info("skill event %s for user %s", kind, _short(event_user))
        remember_user(event_user, "skill-event")
        return {}

    log.info("request %s for user %s", kind, _short(user_id))
    remember_user(user_id, "request")

    try:
        if kind == "LaunchRequest":
            return status_reply()
        if kind == "IntentRequest":
            return on_intent(request)
        if kind == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        return speak("I didn't catch that. " + REPROMPT, reprompt=REPROMPT)
    except RelayError:
        log.exception("Relay call failed")
        return speak("I couldn't reach SHIP just now. Please try again in a moment.", end=True)
