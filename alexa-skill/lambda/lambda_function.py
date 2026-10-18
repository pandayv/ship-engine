"""
SHIP Review: the Alexa skill that stands in for an Alexa+ add-on.

Alexa+ add-ons are not open to hackathon participants, so a custom skill
carries the same conversation: Alexa hears the person, and this function
turns what they said into calls to Relay, SHIP's MCP server. Relay decides
everything that matters (what to say, what a voice may do, where a finding
is shown). This file only translates, so voice still goes through the MCP
server.

Two ways in. The common requests have their own Alexa intents and are
answered by plain code, fast. Anything else the person says in their own
words arrives as free text and goes to orchestrator.py, where a language
model chooses the Relay tools (and plain code checks its choice).

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

import orchestrator

log = logging.getLogger()
log.setLevel(logging.INFO)

RELAY_URL = os.environ.get(
    "SHIP_RELAY_URL",
    "https://4f5g7mxyhhhhctmbagr5syfloa0airkl.lambda-url.us-west-2.on.aws/mcp",
)
USERS_TABLE = os.environ.get("SHIP_ALEXA_USERS_TABLE", "ship-alexa-users")
MODEL_ID = os.environ.get("SHIP_ALEXA_MODEL_ID", "amazon.nova-lite-v1:0")
CONSOLE_URL = os.environ.get("SHIP_CONSOLE_URL", "")
PROTOCOL_VERSION = "2025-11-25"
RELAY_TIMEOUT_SECONDS = 5
# Alexa abandons the skill at 8 seconds. Stop waiting a little before that so
# the person hears our apology instead of Alexa's generic failure message.
ANSWER_BY_SECONDS = 7.0

# Marker for "say whatever is the useful next step right now". The words come from
# next_step(), which looks at where the conversation is.
REPROMPT = object()

# Intents that carry free text, and the verb to restore in front of it. The
# general intent has only neutral carriers ("can you ...", "please ..."), so
# nothing is lost there.
FREE_TEXT_INTENTS = {
    "AskIntent": "",
    "ShowFreeIntent": "show ",
    "ApproveIntent": "approve ",
    "RejectIntent": "reject ",
    "MarkIntent": "mark ",
    "WhatIntent": "what is ",
}

# One MCP session per warm container. Relay is stateless, so this is the
# handshake a client is expected to perform, done once instead of per request.
_mcp = {"ready": False, "protocol": None, "tools": [], "tool_defs": [], "next_id": 0}
_table_handle = None
_bedrock_handle = None
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
    _mcp["tool_defs"] = listed.get("tools", [])
    _mcp["tools"] = [tool["name"] for tool in _mcp["tool_defs"]]
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

def next_step(session):
    """The one useful thing to suggest right now, from where the conversation is. Never
    names a screen that is not set up."""
    session = session or {}
    if session.get("routing"):
        return "Say approve or reject, with the number and a reason."
    screens = session.get("screens")
    if screens:
        return f"You can ask what's up, or say show it on the {screens[0]}."
    return "You can ask what's up."


def speak(text, end=False, reprompt=None, session=None, card=None):
    if reprompt is REPROMPT:
        reprompt = next_step(session)
    response = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if card:
        response["card"] = card
    if reprompt and not end:
        response["reprompt"] = {"outputSpeech": {"type": "PlainText", "text": reprompt}}
    return {"version": "1.0", "sessionAttributes": session or {}, "response": response}


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


# ----------------------------------------------------------------- the model

def _converse(system, messages, tools):
    """One call to the model. Short timeouts: Alexa gives the whole skill 8 seconds."""
    global _bedrock_handle
    if _bedrock_handle is None:
        import boto3
        from botocore.config import Config

        _bedrock_handle = boto3.client(
            "bedrock-runtime",
            config=Config(connect_timeout=1.5, read_timeout=3, retries={"max_attempts": 1}),
        )
    return _bedrock_handle.converse(
        modelId=MODEL_ID,
        system=[{"text": system}],
        messages=messages,
        toolConfig=tools,
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )


# ------------------------------------------------------------------- intents

def _session(event):
    """The conversation state Alexa hands back to us on every turn."""
    attributes = (event.get("session") or {}).get("attributes") or {}
    return {
        "history": list(attributes.get("history", [])),
        "routing": list(attributes.get("routing", [])),
        "screen": attributes.get("screen"),
        "screens": attributes.get("screens"),
        "pending": attributes.get("pending"),
    }


def status_reply(session):
    connected_screens(session)
    sentence = str(call_tool("release_status"))
    orchestrator.remember(session, "what's the status", sentence)
    return speak(sentence, reprompt=REPROMPT, session=session)


def connected_screens(session=None):
    """The names of the screens that are set up right now, or None if that could not be checked.
    Remembered in the session so a reprompt can name a screen that really exists."""
    try:
        screens = list(call_tool("connected_screens").get("screens", []))
    except Exception:  # noqa: BLE001 - not knowing must not stop the answer
        log.exception("could not list the connected screens")
        return None
    if session is not None:
        session["screens"] = screens
    return screens


def seen_routing():
    """Which findings the person has actually seen on a screen, according to Relay, as
    [{repo, pr_number, findings}] for each pull request with at least one seen finding.
    None if that could not be checked. This is the source of truth: the skill's own
    memory of what it displayed is not, because the person may have opened a notification
    on a device the skill never talked to."""
    try:
        data = call_tool("findings_routing")
    except Exception:  # noqa: BLE001
        log.exception("could not read which findings were seen")
        return None
    routing = []
    for pr in data.get("pull_requests", []):
        if any(f.get("seen") for f in pr.get("findings", [])):
            routing.append({
                "repo": pr["repo"], "pr_number": pr["pr_number"],
                "findings": [{"position": f["position"], "alert_id": f["alert_id"],
                              "blocks_merge": f.get("blocks_merge")} for f in pr["findings"]],
            })
    return routing


def show_reply(slots, session):
    screen = _slot_value(slots, "screen")
    connected = connected_screens(session)
    card = orchestrator.console_card(CONSOLE_URL) if connected == [] and CONSOLE_URL else None
    if not screen and connected and len(connected) == 1:
        screen = connected[0]       # only one to choose from, so no need to ask
    if not screen:
        return speak(orchestrator.which_screen(connected), reprompt="Which screen?", session=session, card=card)
    if connected is not None:
        registered = orchestrator.match_connected(screen, connected)
        if registered is None:
            return speak(orchestrator.not_connected(screen, connected), reprompt=REPROMPT, session=session, card=card)
        screen = registered     # the name the screen actually registered under
    result = call_tool("display_on", {"surface": screen})
    if result.get("delivered") or result.get("notified"):
        session["screen"] = screen
    if result.get("delivered"):
        session["routing"] = result.get("routing") or []
    sentence = result.get("spoken_response", "Okay.")
    orchestrator.remember(session, f"show it on the {screen}", sentence)
    if result.get("delivered"):
        sentence += " Say approve or reject, with the number and a reason."
    return speak(sentence, reprompt=REPROMPT, session=session)


def free_text_reply(text, session):
    """Anything said in the person's own words. A language model picks the tools;
    if it can't be used in time, plain rules cover the common requests."""
    _connect()
    connected = connected_screens(session)
    seen = seen_routing()
    if seen is not None:
        session["routing"] = seen       # what Relay says was seen, not what we remember
    try:
        reply = orchestrator.run_turn(
            text, session, _mcp["tool_defs"], call_tool, _converse, _deadline,
            connected=connected, console_url=CONSOLE_URL,
        )
    except orchestrator.OrchestratorUnavailable:
        log.exception("model unavailable, using the simple rules")
        reply = orchestrator.fallback_turn(text, session, call_tool, connected=connected, console_url=CONSOLE_URL)
        orchestrator.remember(session, text, reply.speech)
    return speak(reply.speech, reprompt=REPROMPT, session=session, card=reply.card)


NOTIFICATIONS_SCOPE = "alexa::devices:all:notifications:write"


def alerts_reply(session):
    """Asks for the notification permission. Alexa shows the request as a card in the Alexa
    app, where the person grants it; nothing can grant it by voice."""
    return speak(
        "To tell you when something is found, I need your permission. "
        "I've put a request in your Alexa app. Open the app and allow notifications.",
        reprompt=REPROMPT, session=session,
        card={"type": "AskForPermissionsConsent", "permissions": [NOTIFICATIONS_SCOPE]},
    )


def on_intent(request, session):
    name = request["intent"]["name"]
    slots = request["intent"].get("slots")
    log.info("intent %s heard %s", name,
             {k: v.get("value") for k, v in (slots or {}).items() if isinstance(v, dict)})
    if name == "StatusIntent":
        return status_reply(session)
    if name == "ShowIntent":
        return show_reply(slots, session)
    if name in FREE_TEXT_INTENTS:
        spoken = ((slots or {}).get("request") or {}).get("value")
        if spoken:
            # Alexa gives us only the slot. For the verb intents the verb was the
            # carrier phrase, so put it back or "reject the first one" would
            # reach the model as "the first one".
            return free_text_reply(f"{FREE_TEXT_INTENTS[name]}{spoken}", session)
        return speak("What would you like to do?", reprompt=REPROMPT, session=session)
    if name == "AlertsIntent":
        return alerts_reply(session)
    if name == "AMAZON.YesIntent":
        return free_text_reply("yes", session)
    if name == "AMAZON.NoIntent":
        return free_text_reply("no", session)
    if name == "AMAZON.HelpIntent":
        return speak(
            "I tell you whether anything is blocked, and I put the detail on a screen. "
            "You can also say what you want in your own words, like approve the first one because it's a false positive. "
            "Say turn on alerts to be told when something is found.",
            reprompt=REPROMPT, session=session,
        )
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
        return speak("Okay.", end=True)
    # Alexa could not match what was said to anything it knows. It hands over no text,
    # so say what does work instead of guessing.
    log.info("no intent matched: %s", name)
    return speak("Sorry, I missed that. " + next_step(session), reprompt=REPROMPT, session=session)


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
    session = _session(event)

    try:
        if kind == "LaunchRequest":
            return status_reply(session)
        if kind == "IntentRequest":
            return on_intent(request, session)
        if kind == "SessionEndedRequest":
            return {"version": "1.0", "response": {}}
        return speak("Sorry, I missed that. " + next_step(session), reprompt=REPROMPT, session=session)
    except RelayError:
        log.exception("Relay call failed")
        return speak("I couldn't reach SHIP just now. Please try again in a moment.", end=True)
