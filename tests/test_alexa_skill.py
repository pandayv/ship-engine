"""
Tests for the Alexa skill (alexa-skill/lambda/lambda_function.py).

The skill is a translator: Alexa's understanding of what was said in, a call
to Relay out, Relay's own sentence spoken back. These tests use a fake Relay
that records every JSON-RPC message, so they need no network or credentials.
"""

import importlib.util
import json
from pathlib import Path

import pytest

SKILL_PATH = Path(__file__).resolve().parent.parent / "alexa-skill" / "lambda" / "lambda_function.py"
STATUS_SENTENCE = "2 blockers found. Ready for your decision. Want it on a screen?"
TOOLS = [
    "release_status", "blocked_pull_requests", "finding_detail",
    "request_risk_acceptance", "stage_decisions", "proceed", "display_on",
]


@pytest.fixture
def skill(monkeypatch):
    """A fresh copy of the skill module with a fake Relay and a recording user store."""
    spec = importlib.util.spec_from_file_location("ship_alexa_skill", SKILL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.sent = []          # every JSON-RPC message the skill sent, in order
    module.saved = []         # every (user_id, source) the skill tried to remember
    module.fail_relay = False
    module.display_reply = {"spoken_response": "Putting it on your tv."}

    def fake_post(body, protocol=None):
        if module.fail_relay:
            raise module.RelayError("down")
        module.sent.append((body.get("method"), body.get("params", {}).get("name"), protocol))
        method = body.get("method")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-11-25"}}
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": n} for n in TOOLS]}}
        if method == "tools/call":
            name = body["params"]["name"]
            text = STATUS_SENTENCE if name == "release_status" else json.dumps(module.display_reply)
            return {"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr(module, "_post", fake_post)
    monkeypatch.setattr(module, "remember_user", lambda uid, src: module.saved.append((uid, src)))
    return module


def request_event(request, user_id="amzn1.ask.account.TESTUSER"):
    return {"context": {"System": {"user": {"userId": user_id}}}, "request": request}


def intent(name, slots=None):
    return {"type": "IntentRequest", "intent": {"name": name, "slots": slots or {}}}


def spoken(reply):
    return reply["response"]["outputSpeech"]["text"]


def matched_screen(screen_id):
    return {"screen": {"name": "screen", "value": screen_id, "resolutions": {"resolutionsPerAuthority": [
        {"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"id": screen_id, "name": screen_id}}]}]}}}


def test_opening_the_skill_speaks_relays_own_sentence_and_stays_open(skill):
    reply = skill.handler(request_event({"type": "LaunchRequest"}), None)
    assert spoken(reply) == STATUS_SENTENCE
    assert reply["response"]["shouldEndSession"] is False
    assert "reprompt" in reply["response"]


def test_status_intent_speaks_the_same_sentence(skill):
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    assert spoken(reply) == STATUS_SENTENCE


def test_the_mcp_handshake_runs_in_order_and_only_once(skill):
    skill.handler(request_event(intent("StatusIntent")), None)
    skill.handler(request_event(intent("StatusIntent")), None)
    methods = [m for m, _, _ in skill.sent]
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"]


def test_calls_after_initialize_carry_the_negotiated_protocol_version(skill):
    skill.handler(request_event(intent("StatusIntent")), None)
    after_init = [p for m, _, p in skill.sent if m != "initialize"]
    assert after_init and all(p == "2025-11-25" for p in after_init)


def test_show_intent_sends_the_resolved_screen_to_display_on(skill):
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert ("tools/call", "display_on", "2025-11-25") in skill.sent
    assert spoken(reply) == "Putting it on your tv."


def test_show_intent_without_a_matched_screen_asks_and_does_not_push(skill):
    reply = skill.handler(request_event(intent("ShowIntent", {"screen": {"name": "screen", "value": "banana"}})), None)
    assert "Which screen" in spoken(reply)
    assert not any(name == "display_on" for _, name, _ in skill.sent)


def test_unmatched_synonym_is_not_trusted_as_a_screen_name(skill):
    # Alexa heard something outside the SCREEN list: the raw words must not reach Relay.
    skill.handler(request_event(intent("ShowIntent", {"screen": {"name": "screen", "value": "garage"}})), None)
    assert not any(name == "display_on" for _, name, _ in skill.sent)


def test_a_dead_relay_becomes_a_spoken_apology_not_an_exception(skill):
    skill.fail_relay = True
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    assert "couldn't reach SHIP" in spoken(reply)
    assert reply["response"]["shouldEndSession"] is True


def test_a_tool_relay_does_not_list_is_reported_not_called(skill):
    skill._connect()
    skill._mcp["tools"].remove("display_on")
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert "couldn't reach SHIP" in spoken(reply)
    assert not any(name == "display_on" for _, name, _ in skill.sent)


def test_stop_ends_the_session(skill):
    reply = skill.handler(request_event(intent("AMAZON.StopIntent")), None)
    assert reply["response"]["shouldEndSession"] is True


def test_session_ended_returns_without_speech(skill):
    reply = skill.handler(request_event({"type": "SessionEndedRequest"}), None)
    assert "outputSpeech" not in reply["response"]


def test_every_request_remembers_the_user(skill):
    skill.handler(request_event({"type": "LaunchRequest"}, user_id="amzn1.ask.account.AAA"), None)
    assert skill.saved == [("amzn1.ask.account.AAA", "request")]


def test_a_skill_event_remembers_the_user_and_returns_an_empty_object(skill):
    event = {"context": {"System": {}}, "request": {
        "type": "AlexaSkillEvent.ProactiveSubscriptionChanged", "userId": "amzn1.ask.account.BBB"}}
    assert skill.handler(event, None) == {}
    assert skill.saved == [("amzn1.ask.account.BBB", "skill-event")]


def test_a_failure_to_save_the_user_never_breaks_the_answer(skill, monkeypatch):
    # Use the real remember_user, with a table that explodes.
    spec = importlib.util.spec_from_file_location("ship_alexa_skill_real", SKILL_PATH)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    monkeypatch.setattr(real, "_users_table", lambda: (_ for _ in ()).throw(RuntimeError("no table")))
    real.remember_user("amzn1.ask.account.CCC", "request")   # must not raise


def test_the_skill_and_the_storage_module_agree_on_the_table(skill):
    from src.storage import alexa_user_store

    assert skill.USERS_TABLE == alexa_user_store.TABLE_NAME
    source = SKILL_PATH.read_text()
    assert f'"{alexa_user_store.KEY}": user_id' in source   # the key attribute the skill writes


def test_the_interaction_model_and_the_screen_names_relay_understands_line_up():
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    language = model["interactionModel"]["languageModel"]
    assert language["invocationName"] == "ship review"
    screens = {v["id"] for t in language["types"] if t["name"] == "SCREEN" for v in t["values"]}
    assert screens == {"tv", "ipad", "laptop", "fridge"}       # "here" is excluded: nothing renders on an Echo yet
    assert {"StatusIntent", "ShowIntent"} <= {i["name"] for i in language["intents"]}


def test_relay_calls_stop_before_alexas_eight_second_limit(skill, monkeypatch):
    # Use the real _post with a network that must never be touched once time is up.
    spec = importlib.util.spec_from_file_location("ship_alexa_skill_deadline", SKILL_PATH)
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)

    def no_network(*args, **kwargs):
        raise AssertionError("must not call Relay after the deadline")

    monkeypatch.setattr(real.urllib.request, "urlopen", no_network)
    real._deadline = real.time.time() - 1
    with pytest.raises(real.RelayError):
        real._post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_the_deadline_leaves_room_before_alexas_limit(skill):
    assert skill.ANSWER_BY_SECONDS < 8
    assert skill.RELAY_TIMEOUT_SECONDS < skill.ANSWER_BY_SECONDS
