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
import sys
sys.path.insert(0, str(SKILL_PATH.parent))   # the skill imports orchestrator.py from its own folder
STATUS_SENTENCE = "2 blockers found. Ready for your decision. Want it on a screen?"
TOOLS = [
    "release_status", "blocked_pull_requests", "finding_detail",
    "request_risk_acceptance", "stage_decisions", "proceed", "display_on", "connected_screens",
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
    module.connected = ["tv", "laptop"]

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
            if name == "connected_screens":
                text = json.dumps({"screens": module.connected})
            else:
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
    assert screens == {"tv", "tablet", "laptop", "fridge"}       # "here" is excluded: nothing renders on an Echo yet
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


# --- free text: the verb that was the carrier phrase is put back ---


def spoken_to_model(skill, monkeypatch, intent_name, said):
    """Runs one free-text intent and returns the exact sentence handed to the model layer."""
    captured = {}
    monkeypatch.setattr(skill, "free_text_reply", lambda text, session: captured.update(text=text) or skill.speak("ok", session=session))
    skill.handler(request_event(intent(intent_name, {"request": {"name": "request", "value": said}})), None)
    return captured["text"]


def test_the_general_intent_passes_the_persons_words_unchanged(skill, monkeypatch):
    assert spoken_to_model(skill, monkeypatch, "AskIntent", "tell me how many blockers there are") == "tell me how many blockers there are"


@pytest.mark.parametrize("intent_name,verb", [("ApproveIntent", "approve "), ("RejectIntent", "reject "), ("ShowFreeIntent", "show ")])
def test_the_verb_a_carrier_phrase_swallowed_is_restored(skill, monkeypatch, intent_name, verb):
    said = "the first one because it is test data"
    assert spoken_to_model(skill, monkeypatch, intent_name, said) == verb + said


def test_the_free_text_intents_are_all_in_the_language_model():
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    intents = {i["name"]: i for i in model["interactionModel"]["languageModel"]["intents"]}
    module_spec = importlib.util.spec_from_file_location("ship_alexa_skill_names", SKILL_PATH)
    mod = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(mod)
    for name in mod.FREE_TEXT_INTENTS:
        assert name in intents and intents[name]["slots"][0]["type"] == "AMAZON.SearchQuery"


def test_no_verb_is_used_as_a_carrier_in_the_general_intent():
    # If it were, Alexa would drop the verb and the model could not tell approve from reject.
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    ask = next(i for i in model["interactionModel"]["languageModel"]["intents"] if i["name"] == "AskIntent")
    verbs = {"approve", "accept", "reject", "dismiss", "confirm", "clear", "ignore", "block", "keep", "show", "put", "send", "mark", "yes", "no"}
    for sample in ask["samples"]:
        carrier = sample.replace("{request}", "").split()
        assert not (verbs & set(carrier)), sample


# --- only screens that are really set up are offered ---


def test_show_it_on_a_screen_that_is_not_connected_says_which_ones_are(skill):
    skill.connected = ["laptop"]
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert spoken(reply) == "I don't see a tv connected. I can see the laptop."
    assert not any(name == "display_on" for _, name, _ in skill.sent)


def test_with_no_screen_at_all_the_person_hears_so_and_gets_a_card(skill, monkeypatch):
    skill.connected = []
    monkeypatch.setattr(skill, "CONSOLE_URL", "https://example.test/dashboard")
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert "No screen is set up" in spoken(reply)
    assert "https://example.test/dashboard" in reply["response"]["card"]["content"]


def test_asking_which_screen_offers_only_connected_ones(skill):
    skill.connected = ["laptop", "tablet"]
    reply = skill.handler(request_event(intent("ShowIntent", {})), None)
    assert spoken(reply) == "Which screen? I can see the laptop and the tablet."


def test_if_the_screen_list_cannot_be_read_the_answer_still_works(skill, monkeypatch):
    original = skill.call_tool
    def flaky(name, arguments=None):
        if name == "connected_screens":
            raise skill.RelayError("down")
        return original(name, arguments)
    monkeypatch.setattr(skill, "call_tool", flaky)
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert spoken(reply) == "Putting it on your tv."


def test_asking_to_show_it_with_exactly_one_screen_connected_just_uses_it(skill):
    skill.connected = ["laptop"]
    skill.display_reply = {"delivered": True, "spoken_response": "Putting it on your laptop.", "routing": []}
    reply = skill.handler(request_event(intent("ShowIntent", {})), None)
    assert spoken(reply) == "Putting it on your laptop."
    assert ("tools/call", "display_on", "2025-11-25") in skill.sent


def test_turning_on_alerts_sends_the_permission_request_card_to_the_app(skill):
    reply = skill.handler(request_event(intent("AlertsIntent")), None)
    card = reply["response"]["card"]
    assert card == {"type": "AskForPermissionsConsent", "permissions": ["alexa::devices:all:notifications:write"]}
    assert "Alexa app" in spoken(reply)


def test_the_permission_asked_for_is_the_one_the_manifest_declares():
    manifest = json.loads((SKILL_PATH.parent.parent / "skill-package" / "skill.json").read_text())["manifest"]
    declared = {p["name"] for p in manifest["permissions"]}
    module_spec = importlib.util.spec_from_file_location("ship_alexa_skill_scope", SKILL_PATH)
    mod = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(mod)
    assert mod.NOTIFICATIONS_SCOPE in declared
