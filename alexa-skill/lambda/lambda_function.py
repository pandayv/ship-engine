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
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import orchestrator
import review_flow

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
    "AllowIntent": "allow ",
    "BlockIntent": "block ",
    "MarkIntent": "mark ",
    "WhatIntent": "what is ",
}

# One MCP session per warm container. Relay is stateless, so this is the
# handshake a client is expected to perform, done once instead of per request.
_mcp = {"ready": False, "protocol": None, "tools": [], "tool_defs": [], "next_id": 0}
_table_handle = None
_bedrock_handle = None
_deadline = float("inf")   # set at the start of every request by handler()
_id_lock = threading.Lock()


class RelayError(Exception):
    """Relay could not be reached or answered with an error."""


class RelayToolError(RelayError):
    """Relay was reached but the tool failed (the MCP reply carried isError). The text of
    such a reply is an exception message, not a sentence for a person: it is logged and
    never spoken."""


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
    with _id_lock:
        _mcp["next_id"] += 1
        request_id = _mcp["next_id"]
    reply = _post(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
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
    if result.get("isError"):
        # FastMCP turns an exception inside a tool into text like "Error executing tool ...".
        # That is for the logs, never for the speaker.
        log.error("Relay tool %s failed: %s", name, str(text)[:300])
        raise RelayToolError(f"tool {name} failed")
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

ELICIT_ANSWER = {
    "type": "Dialog.ElicitSlot", "slotToElicit": "answer",
    "updatedIntent": {"name": "AnswerIntent", "confirmationStatus": "NONE",
                      "slots": {"answer": {"name": "answer", "confirmationStatus": "NONE"}}},
}


def speak_turn(turn, session):
    """Speaks what the review flow says next. A question is asked so that Alexa hands over
    whatever the person says in reply as plain words, instead of matching it against the
    skill's sample phrases and giving up on anything that doesn't fit."""
    if turn.elicit:
        return speak(turn.speech, reprompt=turn.reprompt, session=session, card=turn.card, directives=[ELICIT_ANSWER])
    return speak(turn.speech, reprompt=REPROMPT, session=session, card=turn.card)


def next_step(session):
    """The one useful thing to suggest right now, from where the conversation is. Never
    names a screen that is not set up."""
    session = session or {}
    if session.get("routing"):
        return "Say allow or block, with the number and a reason."
    screens = session.get("screens")
    if screens:
        return f"You can ask what's up, or say show it on the {' or '.join(screens[:2])}."
    return "You can ask what's up."


def speak(text, end=False, reprompt=None, session=None, card=None, directives=None):
    if reprompt is REPROMPT:
        reprompt = next_step(session)
    response = {"outputSpeech": {"type": "PlainText", "text": text}, "shouldEndSession": end}
    if directives:
        response["directives"] = directives
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
        "flow": attributes.get("flow"),
        "reasons_asked": attributes.get("reasons_asked"),
        "pending": attributes.get("pending"),
    }


def _both(first, second):
    """Two independent Relay reads at once: each is a round trip, and Alexa is waiting."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        return a.result(), b.result()


def status_reply(session):
    _connect()      # the handshake once, before the reads share it
    _, sentence = _both(lambda: connected_screens(session), lambda: call_tool("release_status"))
    sentence = str(sentence)
    orchestrator.remember(session, "what's the status", sentence)
    screens = session.get("screens") or []
    if screens and sentence.rstrip().endswith("?"):
        return speak_turn(review_flow.offer_screens(session, screens, sentence), session)
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
    [{repo, pr_number, findings: [{position, alert_id, blocks_merge}]}] for each pull
    request with at least one seen finding, or None if that could not be checked.

    `position` is the number the finding had on the screen that displayed it (Relay records
    that when it shows the findings), which is the number the person will say. It is NOT
    where the finding sits now: findings that arrive or get decided after the screen was drawn
    move the live numbers, and "the second one" must still mean the one they looked at. Only
    seen findings are included, and a number that two seen findings both claim is dropped
    (ambiguous, so refused rather than guessed).

    This is the source of truth: the skill's own memory of what it displayed is not, because
    the person may have opened a notification on a device the skill never talked to."""
    try:
        data = call_tool("findings_routing")
    except Exception:  # noqa: BLE001
        log.exception("could not read which findings were seen")
        return None
    routing = []
    for pr in data.get("pull_requests", []):
        findings = pr.get("findings", [])
        # A Relay from before shown_position existed only knows the live number.
        legacy = any("shown_position" not in f for f in findings)
        picked = [(f.get("position") if legacy else f.get("shown_position"), f) for f in findings if f.get("seen")]
        picked = [(pos, f) for pos, f in picked if pos is not None]
        counts = {}
        for pos, _ in picked:
            counts[pos] = counts.get(pos, 0) + 1
        kept = [{"position": pos, "alert_id": f["alert_id"], "blocks_merge": f.get("blocks_merge")}
                for pos, f in picked if counts[pos] == 1]
        if kept:
            routing.append({"repo": pr["repo"], "pr_number": pr["pr_number"], "findings": kept})
    return routing


def _show_on(session, screen):
    """Puts the findings on one screen and asks what comes next. Returns a review_flow.Turn."""
    result = call_tool("display_on", {"surface": screen})
    delivered, notified = bool(result.get("delivered")), bool(result.get("notified"))
    if delivered or notified:
        session["screen"] = screen
    if delivered:
        session["routing"] = result.get("routing") or []
    sentence = result.get("spoken_response", "Okay.")
    orchestrator.remember(session, f"show it on the {screen}", sentence)
    return review_flow.offer_after_show(session, sentence, delivered, notified)


def show_reply(slots, session):
    screen = _slot_value(slots, "screen")
    if screen == "all":
        return show_all_reply(session)    # "both", "all of them", "everywhere"
    connected = connected_screens(session)
    card = orchestrator.console_card(CONSOLE_URL) if connected == [] and CONSOLE_URL else None
    if not screen and connected and len(connected) == 1:
        screen = connected[0]       # only one to choose from, so no need to ask
    if not screen:
        # Alexa caught the answer with the show intent but the words are not a screen name
        # ("the first one"). Let the model read them in the light of the conversation
        # instead of asking the same question again.
        spoken = ((slots or {}).get("screen") or {}).get("value")
        if spoken:
            return free_text_reply(str(spoken), session)
        return speak(orchestrator.which_screen(connected), reprompt="Which screen?", session=session, card=card)
    if connected is not None:
        registered = orchestrator.match_connected(screen, connected)
        if registered is None:
            return speak(orchestrator.not_connected(screen, connected), reprompt=REPROMPT, session=session, card=card)
        screen = registered     # the name the screen actually registered under
    return speak_turn(_show_on(session, screen), session)


def _show_all(session):
    """The findings on every screen that is set up. Returns a review_flow.Turn."""
    _connect()
    connected = connected_screens(session)
    if not connected:
        return review_flow.Turn(orchestrator.which_screen(connected))
    with ThreadPoolExecutor(max_workers=len(connected)) as pool:
        results = list(pool.map(lambda name: _safe_display_on(name), connected))
    shown = [n for n, r in zip(connected, results) if r.get("delivered")]
    notified = [n for n, r in zip(connected, results) if not r.get("delivered") and r.get("notified")]
    missed = [n for n, r in zip(connected, results) if not r.get("delivered") and not r.get("notified")]
    if shown or notified:
        session["screen"] = (shown or notified)[0]
    delivered = next((r for r in results if r.get("delivered")), None)
    if delivered:
        session["routing"] = delivered.get("routing") or []
    parts = []
    if shown:
        parts.append(f"Putting it on {orchestrator._join(shown)}.")
    if notified:
        parts.append(f"I've sent a notification to {orchestrator._join(notified)}. Open it to review.")
    if missed:
        parts.append(f"I couldn't reach {orchestrator._join(missed)}.")
    sentence = " ".join(parts)
    orchestrator.remember(session, "show it on all of them", sentence)
    return review_flow.offer_after_show(session, sentence, bool(shown), bool(notified))


def show_all_reply(session):
    """"Both" or "all of them" in answer to a choice of screens: every screen that is set up."""
    connected = connected_screens(session)
    if connected == [] and CONSOLE_URL:
        card = orchestrator.console_card(CONSOLE_URL)
        return speak(orchestrator.which_screen(connected), reprompt="Which screen?", session=session, card=card)
    return speak_turn(_show_all(session), session)


def _safe_display_on(name):
    try:
        return call_tool("display_on", {"surface": name})
    except Exception:  # noqa: BLE001 - one screen failing must not cancel the others
        log.exception("could not show on %s", name)
        return {}


def _flow_context(session):
    """What the review flow needs from the skill, so it can stay free of Alexa and AWS."""
    from types import SimpleNamespace

    return SimpleNamespace(
        call_tool=call_tool,
        seen_routing=lambda: seen_routing() or [],
        show=lambda name: _show_on(session, name),
        show_all=lambda: _show_all(session),
    )


def start_review(session):
    """Begins walking through what the person has seen on a screen."""
    _connect()
    routing = seen_routing()
    if routing is None:
        return speak("I couldn't check what you've looked at. Please try again.", reprompt=REPROMPT, session=session)
    session["routing"] = routing
    return speak_turn(review_flow.begin(session, routing), session)


STATUS_ASK = re.compile(r"\b(what'?s up|whats up|what is up|status|any blockers|anything blocked|is anything blocked|can we ship|how are we doing)\b", re.I)
ALERTS_ASK = re.compile(r"\b(turn on|enable|set up) (alerts|notifications)\b", re.I)
EVERY_SCREEN = re.compile(r"\b(both|all of them|all screens|every screen|everywhere)\b", re.I)


def command_from_words(text, session):
    """A plain command that reached us as free words, because Alexa was in the middle of one of
    its own questions and hands over everything said then as plain words. These do not need a
    language model, and they must behave exactly as they do when Alexa matches them itself.
    Returns a response, or None if the words are something else."""
    said = text or ""
    short = len(said.split()) <= 8
    if short and ALERTS_ASK.search(said):
        return alerts_reply(session)
    if short and STATUS_ASK.search(said):
        return status_reply(session)
    if short and orchestrator.WANTS_TO_SEE.search(said):
        named = orchestrator.screens_named(said)
        if EVERY_SCREEN.search(said) and not named:
            return show_all_reply(session)
        if len(named) == 1:
            connected = connected_screens(session)
            registered = orchestrator.match_connected(next(iter(named)), connected) if connected else None
            if registered:
                return speak_turn(_show_on(session, registered), session)
        if len(named) > 1:
            return show_all_reply(session)
    return None


START_REVIEW = re.compile(r"\b(start|begin|go through|walk me through|review them|run through|step through|one by one|go over)\b", re.I)


def free_text_reply(text, session):
    """Anything said in the person's own words. A language model picks the tools;
    if it can't be used in time, plain rules cover the common requests."""
    if START_REVIEW.search(text or "") and len((text or "").split()) <= 6:
        return start_review(session)      # "let's go through them"
    command = command_from_words(text, session)
    if command is not None:
        return command
    _connect()
    connected, seen = _both(lambda: connected_screens(session), seen_routing)
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


def drop_pending(session):
    """Leaving the conversation drops a decision that was staged but never confirmed, so
    the screen stops showing it as pending. The goodbye does not wait on this."""
    pending = session.get("pending")
    if not pending:
        return
    try:
        args = {"repo": pending["repo"], "pr_number": pending["pr_number"]}
        if session.get("screen"):
            args["device"] = session["screen"]
        call_tool("cancel_pending", args)
    except Exception:  # noqa: BLE001
        log.exception("could not clear the staged decision on Relay")
    session.pop("pending", None)


def on_intent(request, session):
    name = request["intent"]["name"]
    slots = request["intent"].get("slots")
    log.info("intent %s heard %s", name,
             {k: v.get("value") for k, v in (slots or {}).items() if isinstance(v, dict)})

    def flow_answer(text):
        return speak_turn(review_flow.answer(session, text, _flow_context(session)), session)

    # An answer to a question the skill asked arrives as plain words, whatever was said.
    if name == "AnswerIntent":
        heard = ((slots or {}).get("answer") or {}).get("value") or ""
        return flow_answer(heard) if review_flow.active(session) else free_text_reply(heard, session)

    # While a review is under way every answer belongs to it, whichever intent Alexa chose.
    if review_flow.active(session):
        if name == "AMAZON.YesIntent":
            return flow_answer("yes")
        if name == "AMAZON.NoIntent":
            return flow_answer("no")
        if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
            return flow_answer("stop")
        if name == "AMAZON.HelpIntent":
            return flow_answer("help")
        if name in FREE_TEXT_INTENTS:
            spoken = ((slots or {}).get("request") or {}).get("value")
            return flow_answer(f"{FREE_TEXT_INTENTS[name]}{spoken}" if spoken else FREE_TEXT_INTENTS[name].strip())
        if name == "AMAZON.FallbackIntent":
            again = review_flow.repeat(session)
            if again is not None:
                return speak_turn(again, session)

    if name == "StatusIntent":
        return status_reply(session)
    if name == "StartReviewIntent":
        return start_review(session)
    if name == "ShowIntent":
        return show_reply(slots, session)
    if name in FREE_TEXT_INTENTS:
        spoken = ((slots or {}).get("request") or {}).get("value")
        if spoken:
            # Alexa gives us only the slot. For the verb intents the verb was the
            # carrier phrase, so put it back or "reject the first one" would
            # reach the model as "the first one". The canonical verb is restored, so
            # "approve" and "dismiss" reach the model as "allow", and "reject" as "block".
            return free_text_reply(f"{FREE_TEXT_INTENTS[name]}{spoken}", session)
        if name in ("AllowIntent", "BlockIntent"):
            return start_review(session)      # "allow it" with nothing said about which: go through them
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
            "Then I can go through the findings with you, one at a time. "
            "Say turn on alerts to be told when something is found.",
            reprompt=REPROMPT, session=session,
        )
    if name in ("AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"):
        drop_pending(session)
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
            log.info("session ended: reason=%s error=%s", request.get("reason"), json.dumps(request.get("error")))
            return {"version": "1.0", "response": {}}
        return speak("Sorry, I missed that. " + next_step(session), reprompt=REPROMPT, session=session)
    except RelayToolError:
        log.exception("Relay tool failed")
        # Reached, but it could not do that. Stay in the conversation: a staged decision
        # is still staged and a plain yes will retry it.
        return speak("Something went wrong on my side. Please try again in a moment.",
                     reprompt=REPROMPT, session=session)
    except RelayError:
        log.exception("Relay call failed")
        return speak("I couldn't reach SHIP just now. Please try again in a moment.", end=True)
