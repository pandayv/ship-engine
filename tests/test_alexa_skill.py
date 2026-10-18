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
    "request_risk_acceptance", "stage_decisions", "proceed", "cancel_pending", "display_on", "connected_screens",
    "findings_routing",
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
    module.routing_reply = []
    module.tool_error = None      # name of a tool whose call comes back as an MCP tool error
    module.calls = []             # (tool name, arguments) in order
    module.display_by_surface = {}   # per-screen answers for display_on, when a test needs them

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
            module.calls.append((name, body["params"].get("arguments")))
            if name == module.tool_error:
                return {"jsonrpc": "2.0", "id": body["id"], "result": {
                    "isError": True, "content": [{"type": "text", "text": f"Error executing tool {name}: KeyError 'boom' at server.py:123"}]}}
            if name == "connected_screens":
                text = json.dumps({"screens": module.connected})
            elif name == "findings_routing":
                text = json.dumps({"pull_requests": module.routing_reply})
            elif name == "stage_decisions":
                text = json.dumps({"staged": len((body["params"].get("arguments") or {}).get("decisions") or []), "spoken_response": "Got it."})
            elif name in ("proceed", "cancel_pending"):
                text = json.dumps({"performed": 1, "spoken_response": "Done."})
            elif name == "display_on" and (body["params"].get("arguments") or {}).get("surface") in module.display_by_surface:
                text = json.dumps(module.display_by_surface[body["params"]["arguments"]["surface"]])
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
    # each turn asks which screens exist, then for the status
    assert methods == ["initialize", "notifications/initialized", "tools/list"] + ["tools/call"] * 4
    assert sorted(n for n, _ in skill.calls) == ["connected_screens"] * 2 + ["release_status"] * 2


def test_calls_after_initialize_carry_the_negotiated_protocol_version(skill):
    skill.handler(request_event(intent("StatusIntent")), None)
    after_init = [p for m, _, p in skill.sent if m != "initialize"]
    assert after_init and all(p == "2025-11-25" for p in after_init)


def test_show_intent_sends_the_resolved_screen_to_display_on(skill):
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("tv"))), None)
    assert ("tools/call", "display_on", "2025-11-25") in skill.sent
    assert spoken(reply) == "Putting it on your tv."


def test_show_intent_with_no_screen_at_all_asks_which_and_does_not_push(skill):
    reply = skill.handler(request_event(intent("ShowIntent", {})), None)
    assert "Which screen" in spoken(reply)
    assert not any(name == "display_on" for _, name, _ in skill.sent)


def test_words_that_are_not_a_screen_are_read_by_the_model_not_asked_about_again(skill, monkeypatch):
    # "the first one", caught by the show intent: it is an answer to something else.
    heard = []
    monkeypatch.setattr(skill, "free_text_reply", lambda text, session: heard.append(text) or skill.speak("ok", session=session))
    skill.handler(request_event(intent("ShowIntent", {"screen": {"name": "screen", "value": "first one"}})), None)
    assert heard == ["first one"]
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
    # "here" is excluded: nothing renders on an Echo yet. "all" is not a screen: it means every one that is set up.
    assert screens == {"tv", "tablet", "laptop", "fridge", "all"}
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


@pytest.mark.parametrize("intent_name,verb", [("AllowIntent", "allow "), ("BlockIntent", "block "), ("ShowFreeIntent", "show ")])
def test_the_verb_a_carrier_phrase_swallowed_is_restored(skill, monkeypatch, intent_name, verb):
    said = "the first one because it is test data"
    assert spoken_to_model(skill, monkeypatch, intent_name, said) == verb + said


def _language_model():
    return json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())["interactionModel"]["languageModel"]


def _intent_verbs(intent_name):
    """The verb in front of the slot in each sample of an intent (the part Alexa swallows)."""
    intents = {i["name"]: i for i in _language_model()["intents"]}
    return [sample.replace("{request}", "").strip() for sample in intents[intent_name]["samples"]]


@pytest.mark.parametrize("verb", ["allow", "approve", "accept", "dismiss", "clear", "ignore"])
def test_every_way_of_saying_allow_still_reaches_the_allow_intent(verb):
    assert verb in _intent_verbs("AllowIntent")


@pytest.mark.parametrize("verb", ["block", "reject", "confirm"])
def test_every_way_of_saying_block_still_reaches_the_block_intent(verb):
    assert verb in _intent_verbs("BlockIntent")


@pytest.mark.parametrize("old_word", ["approve", "accept", "dismiss", "clear", "ignore"])
def test_an_older_word_for_allow_reaches_the_model_as_the_new_word(skill, monkeypatch, old_word):
    # Alexa matched "<old_word> the first one ..." to the allow intent and swallowed the verb. The model sees "allow".
    assert old_word in _intent_verbs("AllowIntent")
    said = "the first one because it is a false positive"
    assert spoken_to_model(skill, monkeypatch, "AllowIntent", said) == "allow " + said


@pytest.mark.parametrize("old_word", ["reject", "confirm"])
def test_an_older_word_for_block_reaches_the_model_as_the_new_word(skill, monkeypatch, old_word):
    assert old_word in _intent_verbs("BlockIntent")
    said = "the second one because it leaks a phone number"
    assert spoken_to_model(skill, monkeypatch, "BlockIntent", said) == "block " + said


def test_the_old_intent_names_are_gone_from_the_model_and_the_skill(skill):
    names = {i["name"] for i in _language_model()["intents"]}
    assert {"AllowIntent", "BlockIntent"} <= names and not ({"ApproveIntent", "RejectIntent"} & names)
    assert not ({"ApproveIntent", "RejectIntent"} & set(skill.FREE_TEXT_INTENTS))
    assert skill.FREE_TEXT_INTENTS["AllowIntent"] == "allow " and skill.FREE_TEXT_INTENTS["BlockIntent"] == "block "


def test_help_gives_an_allow_example_and_the_reprompt_asks_for_allow_or_block(skill):
    reply = skill.handler(request_event(intent("AMAZON.HelpIntent"), ), None)
    assert "go through the findings with you" in spoken(reply)
    assert "approve" not in spoken(reply) and "reject" not in spoken(reply)


def test_after_findings_are_seen_the_reprompt_asks_for_allow_or_block_with_a_number_and_a_reason(skill):
    reply = skill.speak("ok", reprompt=skill.REPROMPT, session={"routing": [{"repo": "r", "pr_number": 1, "findings": []}]})
    prompt = reply["response"]["reprompt"]["outputSpeech"]["text"]
    assert prompt == "Say allow or block, with the number and a reason."


def test_the_free_text_intents_are_all_in_the_language_model():
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    intents = {i["name"]: i for i in model["interactionModel"]["languageModel"]["intents"]}
    module_spec = importlib.util.spec_from_file_location("ship_alexa_skill_names", SKILL_PATH)
    mod = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(mod)
    for name in mod.FREE_TEXT_INTENTS:
        assert name in intents and intents[name]["slots"][0]["type"] == "AMAZON.SearchQuery"


def test_no_verb_is_used_as_a_carrier_in_the_general_intent():
    # If it were, Alexa would drop the verb and the model could not tell allow from block. The verbs are every
    # verb of the allow and block samples (read from the model, so a new synonym is covered automatically) plus
    # the other words that mean a decision or a screen.
    verbs = {"allow", "approve", "accept", "dismiss", "clear", "ignore", "block", "reject", "confirm",
             "keep", "show", "put", "send", "mark", "yes", "no"}
    decision_words = {"allow", "allowed", "approve", "approved", "accept", "dismiss", "clear", "ignore", "block", "blocked",
                      "reject", "rejected", "confirm"}
    verbs |= {word for name in ("AllowIntent", "BlockIntent") for verb in _intent_verbs(name)
              for word in verb.split() if word in decision_words}
    assert {"allow", "approve", "accept", "dismiss", "clear", "ignore", "block", "reject", "confirm"} <= verbs
    ask = next(i for i in _language_model()["intents"] if i["name"] == "AskIntent")
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
    assert spoken(reply) == "Putting it on your laptop. Want to go through them?"
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


# --- what was seen comes from Relay, not from the skill's memory ---


def test_findings_seen_on_another_device_are_known_to_the_next_turn(skill, monkeypatch):
    skill.routing_reply = [{"repo": "pandayv/micro-finance", "pr_number": 1, "findings": [
        {"position": 1, "alert_id": "a1", "blocks_merge": True, "seen": True, "shown_position": 1},
        {"position": 2, "alert_id": "a2", "blocks_merge": True, "seen": False, "shown_position": None}]}]
    routing = skill.seen_routing()
    assert routing[0]["pr_number"] == 1 and [(f["position"], f["alert_id"]) for f in routing[0]["findings"]] == [(1, "a1")]


def test_the_number_is_the_one_the_screen_showed_not_where_the_finding_sits_now(skill):
    # a0 arrived after the screen was drawn and took live number 1; the person's "second one" is still a2.
    skill.routing_reply = [{"repo": "r", "pr_number": 1, "findings": [
        {"position": 1, "alert_id": "a0", "blocks_merge": True, "seen": False, "shown_position": None},
        {"position": 2, "alert_id": "a1", "blocks_merge": True, "seen": True, "shown_position": 1},
        {"position": 3, "alert_id": "a2", "blocks_merge": True, "seen": True, "shown_position": 2}]}]
    findings = skill.seen_routing()[0]["findings"]
    assert {f["position"]: f["alert_id"] for f in findings} == {1: "a1", 2: "a2"}


def test_a_number_claimed_by_two_seen_findings_is_dropped_not_guessed(skill):
    skill.routing_reply = [{"repo": "r", "pr_number": 1, "findings": [
        {"position": 1, "alert_id": "x", "seen": True, "shown_position": 1},
        {"position": 2, "alert_id": "y", "seen": True, "shown_position": 1},
        {"position": 3, "alert_id": "z", "seen": True, "shown_position": 2}]}]
    assert {f["position"]: f["alert_id"] for f in skill.seen_routing()[0]["findings"]} == {2: "z"}


def test_a_finding_that_was_shown_some_other_way_has_no_number_to_point_at(skill):
    skill.routing_reply = [{"repo": "r", "pr_number": 1, "findings": [
        {"position": 1, "alert_id": "x", "seen": True, "shown_position": None}]}]
    assert skill.seen_routing() == []


def test_an_older_relay_without_shown_positions_still_works_on_the_live_numbers(skill):
    skill.routing_reply = [{"repo": "r", "pr_number": 1, "findings": [
        {"position": 1, "alert_id": "x", "seen": True}, {"position": 2, "alert_id": "y", "seen": False}]}]
    assert [(f["position"], f["alert_id"]) for f in skill.seen_routing()[0]["findings"]] == [(1, "x")]


def test_a_pull_request_nobody_has_seen_is_not_offered_for_decisions(skill):
    skill.routing_reply = [{"repo": "r", "pr_number": 2, "findings": [{"position": 1, "alert_id": "x", "seen": False}]}]
    assert skill.seen_routing() == []


def test_if_relay_cannot_say_what_was_seen_the_answer_is_unknown_not_empty(skill, monkeypatch):
    original = skill.call_tool
    monkeypatch.setattr(skill, "call_tool", lambda name, args=None: (_ for _ in ()).throw(skill.RelayError("down")) if name == "findings_routing" else original(name, args))
    assert skill.seen_routing() is None


def test_a_screen_that_was_only_notified_is_remembered_as_the_last_one_used(skill):
    skill.connected = ["tablet"]
    skill.display_reply = {"delivered": False, "notified": True, "spoken_response": "I've sent a notification to your tablet. Open it to review.", "routing": []}
    reply = skill.handler(request_event(intent("ShowIntent", {"screen": {"name": "screen", "value": "tablet", "resolutions": {"resolutionsPerAuthority": [
        {"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"id": "tablet", "name": "tablet"}}]}]}}})), None)
    assert reply["sessionAttributes"]["screen"] == "tablet" and "sent a notification" in spoken(reply)


# --- what the skill suggests next, and what it does when it did not understand ---


def test_the_suggestion_never_names_a_screen_that_is_not_set_up(skill):
    skill.connected = ["laptop", "tablet"]
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    prompt = reply["response"]["reprompt"]["outputSpeech"]["text"]
    assert prompt == "Want it on the laptop or the tablet?"      # the short form, said again if there is no answer


def test_the_suggestion_names_at_most_two_screens(skill):
    skill.connected = ["laptop", "tablet", "tv"]
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    prompt = reply["response"]["reprompt"]["outputSpeech"]["text"]
    assert prompt == "Want it on a screen?"           # more than two: no list


def test_with_no_screen_the_suggestion_does_not_offer_one(skill):
    skill.connected = []
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    assert reply["response"]["reprompt"]["outputSpeech"]["text"] == "You can ask what's up."


def test_once_findings_have_been_seen_the_suggestion_is_to_decide(skill):
    session = {"routing": [{"repo": "r", "pr_number": 1, "findings": []}]}
    assert skill.next_step(session) == "Say allow or block, with the number and a reason."


def test_an_unmatched_phrase_gets_a_useful_answer_not_a_screen_offer(skill):
    skill.connected = []
    reply = skill.handler(request_event(intent("AMAZON.FallbackIntent")), None)
    text = spoken(reply)
    assert text.startswith("Sorry, I missed that.") and "TV" not in text


def test_mark_and_what_is_keep_their_first_word(skill, monkeypatch):
    assert spoken_to_model(skill, monkeypatch, "MarkIntent", "the first one as a false positive") == "mark the first one as a false positive"
    assert spoken_to_model(skill, monkeypatch, "WhatIntent", "wrong with the first one") == "what is wrong with the first one"


# --- a failing tool is never spoken ---


def test_a_tool_error_is_logged_and_never_spoken(skill, caplog):
    skill.tool_error = "release_status"
    with caplog.at_level("ERROR"):
        reply = skill.handler(request_event(intent("StatusIntent")), None)
    text = spoken(reply)
    assert "Error executing" not in text and "KeyError" not in text and "server.py" not in text
    assert text == "Something went wrong on my side. Please try again in a moment."
    assert reply["response"]["shouldEndSession"] is False         # a staged decision survives a hiccup
    assert "KeyError" in caplog.text                                # the detail is for the logs


def test_a_tool_error_is_a_relay_error_to_the_rest_of_the_skill(skill):
    skill._connect()
    skill.tool_error = "display_on"
    with pytest.raises(skill.RelayError):
        skill.call_tool("display_on", {"surface": "tv"})
    with pytest.raises(skill.RelayToolError):
        skill.call_tool("display_on", {"surface": "tv"})


def test_a_tool_error_during_a_confirmation_keeps_the_staged_decision(skill, monkeypatch):
    skill.tool_error = "proceed"
    session = {"pending": {"repo": "r", "pr_number": 1}}
    event = request_event(intent("AMAZON.YesIntent"))
    event["session"] = {"attributes": {"pending": session["pending"], "screen": "tv"}}
    reply = skill.handler(event, None)
    assert reply["sessionAttributes"]["pending"] == {"repo": "r", "pr_number": 1}
    assert "Error executing" not in spoken(reply)


# --- leaving drops what was staged ---


@pytest.mark.parametrize("name", ["AMAZON.StopIntent", "AMAZON.CancelIntent", "AMAZON.NavigateHomeIntent"])
def test_leaving_drops_a_staged_decision_on_relay(skill, name):
    event = request_event(intent(name))
    event["session"] = {"attributes": {"pending": {"repo": "r", "pr_number": 2}, "screen": "tablet"}}
    reply = skill.handler(event, None)
    assert reply["response"]["shouldEndSession"] is True
    assert ("cancel_pending", {"repo": "r", "pr_number": 2, "device": "tablet"}) in skill.calls


def test_leaving_with_nothing_staged_calls_nothing(skill):
    skill.handler(request_event(intent("AMAZON.StopIntent")), None)
    assert skill.calls == []


def test_leaving_still_works_when_relay_is_down(skill):
    skill.fail_relay = True
    event = request_event(intent("AMAZON.StopIntent"))
    event["session"] = {"attributes": {"pending": {"repo": "r", "pr_number": 2}}}
    assert skill.handler(event, None)["response"]["shouldEndSession"] is True


def test_the_word_relay_tells_people_to_say_is_understood_as_a_yes():
    # Relay asks the person to "say proceed". If the language model did not map that word to the
    # yes intent, Alexa would answer "sorry, I missed that" at the last step of a decision.
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    yes = next(i for i in model["interactionModel"]["languageModel"]["intents"] if i["name"] == "AMAZON.YesIntent")
    assert "proceed" in yes["samples"]


# --- short answers to "laptop or tablet?" ---


def test_every_short_way_of_naming_a_screen_is_a_sample_of_the_show_intent():
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())
    show = next(i for i in model["interactionModel"]["languageModel"]["intents"] if i["name"] == "ShowIntent")
    for sample in ("{screen}", "{screen} please", "{screen} is fine", "i'll take the {screen}", "use {screen}"):
        assert sample in show["samples"], sample


def test_both_and_all_of_them_are_a_value_of_the_screen_list_not_an_intent_of_their_own():
    # An intent made only of short phrases becomes Alexa's catch-all for stray single words
    # ("banana" showed the findings on every screen). A value of the screen list cannot.
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())["interactionModel"]["languageModel"]
    assert "ShowAllIntent" not in {i["name"] for i in model["intents"]}
    screens = next(t for t in model["types"] if t["name"] == "SCREEN")
    everything = next(v for v in screens["values"] if v["id"] == "all")
    for word in ("both", "both of them", "all of them", "everywhere"):
        assert word in everything["name"]["synonyms"], word


def test_two_phrases_that_swallow_a_decision_are_not_screen_samples():
    # "yes approve the first one" was caught as a screen named "1st" by "{screen} one".
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())["interactionModel"]["languageModel"]
    show = next(i for i in model["intents"] if i["name"] == "ShowIntent")
    assert "{screen} one" not in show["samples"] and "the {screen} one" not in show["samples"]


def test_both_shows_it_on_every_screen_that_is_set_up(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_reply = {"delivered": True, "spoken_response": "Putting it on your x.", "routing": []}
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("all"))), None)
    pushed = sorted(args["surface"] for name, args in skill.calls if name == "display_on")
    assert pushed == ["laptop", "tablet"]
    assert spoken(reply) == "Putting it on the laptop and the tablet. Want to go through them?"


def test_both_says_which_screens_could_not_be_reached(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_by_surface = {"laptop": {"delivered": True, "routing": []}, "tablet": {"delivered": False, "notified": False}}
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("all"))), None)
    assert spoken(reply) == "Putting it on the laptop. I couldn't reach the tablet. Want to go through them?"


def test_both_says_which_screens_were_only_notified(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_by_surface = {"laptop": {"delivered": True, "routing": []}, "tablet": {"delivered": False, "notified": True}}
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("all"))), None)
    assert "I've sent a notification to the tablet. Open it to review." in spoken(reply)
    assert spoken(reply).startswith("Putting it on the laptop.")


def test_both_with_no_screen_set_up_says_so_and_pushes_nothing(skill):
    skill.connected = []
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("all"))), None)
    assert "No screen is set up" in spoken(reply)
    assert not any(name == "display_on" for name, _ in skill.calls)


def test_both_remembers_the_routing_so_the_next_decision_can_name_a_finding(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_reply = {"delivered": True, "routing": [{"repo": "r", "pr_number": 1, "findings": []}]}
    reply = skill.handler(request_event(intent("ShowIntent", matched_screen("all"))), None)
    assert reply["sessionAttributes"]["routing"] == [{"repo": "r", "pr_number": 1, "findings": []}]


# --- the guided review through the skill ---

SEEN = [{"repo": "r", "pr_number": 1, "findings": [
    {"position": 1, "alert_id": "a1", "seen": True, "shown_position": 1, "blocks_merge": True},
    {"position": 2, "alert_id": "a2", "seen": True, "shown_position": 2, "blocks_merge": True}]}]


def directive_of(reply):
    return (reply["response"].get("directives") or [None])[0]


def answer_event(text):
    return request_event(intent("AnswerIntent", {"answer": {"name": "answer", "value": text}}))


def test_a_question_is_asked_so_that_alexa_hands_back_the_answer_as_plain_words(skill):
    skill.connected = ["laptop", "tablet"]
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    assert directive_of(reply)["type"] == "Dialog.ElicitSlot" and directive_of(reply)["slotToElicit"] == "answer"
    assert directive_of(reply)["updatedIntent"]["name"] == "AnswerIntent"
    assert reply["sessionAttributes"]["flow"]["stage"] == "pick_screen"


def test_a_status_with_no_screen_to_offer_asks_nothing_it_cannot_take_an_answer_to(skill):
    skill.connected = []
    reply = skill.handler(request_event(intent("StatusIntent")), None)
    assert directive_of(reply) is None and "flow" not in {k for k, v in reply["sessionAttributes"].items() if v}


def test_naming_a_screen_in_answer_to_the_status_question_shows_it_there_and_offers_the_review(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_reply = {"delivered": True, "spoken_response": "Putting it on your laptop.", "routing": []}
    first = skill.handler(request_event(intent("StatusIntent")), None)
    session = first["sessionAttributes"]
    reply = skill.handler({**answer_event("laptop"), "session": {"attributes": session}}, None)
    assert [args for name, args in skill.calls if name == "display_on"] == [{"surface": "laptop"}]
    assert spoken(reply) == "Putting it on your laptop. Want to go through them?" and directive_of(reply)


def test_an_answer_in_a_review_is_read_by_the_review_whatever_intent_alexa_chose(skill):
    skill.routing_reply = SEEN
    started = skill.handler(request_event(intent("StartReviewIntent")), None)
    assert spoken(started).endswith("Number 1 on pull request 1. Allow, block, or skip?") and directive_of(started)
    session = started["sessionAttributes"]

    def say(event):
        nonlocal session
        reply = skill.handler({**event, "session": {"attributes": session}}, None)
        session = reply["sessionAttributes"]
        return reply

    assert spoken(say(request_event(intent("AMAZON.YesIntent")))).startswith("Sorry") is False
    reply = say(request_event(intent("AllowIntent", {"request": {"name": "request", "value": "it because it is test data"}})))
    assert [a for n, a in skill.calls if n == "stage_decisions"][0]["decisions"][0]["decision"] == "allow"
    assert spoken(reply).startswith("Got it. Number 2")


def test_an_answer_alexa_could_not_place_repeats_the_question_instead_of_giving_up(skill):
    skill.routing_reply = SEEN
    started = skill.handler(request_event(intent("StartReviewIntent")), None)
    reply = skill.handler({**request_event(intent("AMAZON.FallbackIntent")), "session": {"attributes": started["sessionAttributes"]}}, None)
    assert spoken(reply).startswith("Sorry, I missed that. Number 1 on pull request 1.") and directive_of(reply)


def test_stop_during_a_review_is_a_stop_of_the_review(skill):
    skill.routing_reply = SEEN
    started = skill.handler(request_event(intent("StartReviewIntent")), None)
    reply = skill.handler({**request_event(intent("AMAZON.StopIntent")), "session": {"attributes": started["sessionAttributes"]}}, None)
    assert spoken(reply) == "Okay, we'll stop there." and not reply["response"]["shouldEndSession"]


def test_allow_it_with_nothing_said_about_which_goes_through_the_findings(skill):
    skill.routing_reply = SEEN
    reply = skill.handler(request_event(intent("AllowIntent")), None)
    assert spoken(reply).endswith("Allow, block, or skip?")


def test_starting_a_review_before_anything_has_been_seen_says_to_show_it_first(skill):
    skill.routing_reply = []
    reply = skill.handler(request_event(intent("StartReviewIntent")), None)
    assert "show it on a screen first" in spoken(reply) and directive_of(reply) is None


def test_lets_go_through_them_in_the_persons_words_starts_the_review(skill):
    skill.routing_reply = SEEN
    reply = skill.handler(request_event(intent("AskIntent", {"request": {"name": "request", "value": "go through them"}})), None)
    assert spoken(reply).endswith("Allow, block, or skip?")


def test_an_answer_with_no_review_going_is_read_as_ordinary_speech(skill, monkeypatch):
    heard = []
    monkeypatch.setattr(skill, "free_text_reply", lambda text, session: heard.append(text) or skill.speak("ok", session=session))
    skill.handler(answer_event("show it on the tablet"), None)
    assert heard == ["show it on the tablet"]


def test_the_model_has_a_catch_all_free_answer_with_a_dialog_so_questions_capture_any_reply():
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())["interactionModel"]
    answer = next(i for i in model["languageModel"]["intents"] if i["name"] == "AnswerIntent")
    assert answer["slots"][0]["type"] == "AMAZON.SearchQuery" and "{answer}" in answer["slots"][0]["samples"]
    dialog = next(i for i in model["dialog"]["intents"] if i["name"] == "AnswerIntent")
    assert dialog["slots"][0]["elicitationRequired"] and dialog["delegationStrategy"] == "SKILL_RESPONSE"


def test_no_intent_is_made_only_of_one_word_phrases_because_those_become_alexas_catch_all():
    # Seen live: an intent made of "both", "everywhere" and similar took "banana" and "approve".
    model = json.loads((SKILL_PATH.parent.parent / "skill-package" / "interactionModels" / "custom" / "en-US.json").read_text())["interactionModel"]["languageModel"]
    for intent_ in model["intents"]:
        if intent_["name"].startswith("AMAZON.") or not intent_["samples"] or intent_["name"] in ("StatusIntent",):
            continue
        slotless = [s for s in intent_["samples"] if "{" not in s]
        if slotless and len(slotless) == len(intent_["samples"]):
            assert all(len(s.split()) >= 2 for s in slotless), intent_["name"]


# --- commands that arrive as plain words because Alexa was in the middle of a question ---


@pytest.mark.parametrize("said", ["show it on the laptop", "put it on the laptop please", "send it to my laptop"])
def test_a_show_command_that_arrives_as_free_words_behaves_like_the_show_intent(skill, said):
    skill.connected = ["laptop", "tablet"]
    skill.display_reply = {"delivered": True, "spoken_response": "Putting it on your laptop.", "routing": []}
    reply = skill.handler(answer_event(said), None)
    assert [a for n, a in skill.calls if n == "display_on"] == [{"surface": "laptop"}]
    assert spoken(reply) == "Putting it on your laptop. Want to go through them?" and directive_of(reply)


def test_show_it_on_both_as_free_words_goes_to_every_screen(skill):
    skill.connected = ["laptop", "tablet"]
    skill.display_reply = {"delivered": True, "spoken_response": "x", "routing": []}
    skill.handler(answer_event("show it on both"), None)
    assert sorted(a["surface"] for n, a in skill.calls if n == "display_on") == ["laptop", "tablet"]


@pytest.mark.parametrize("said", ["what's up", "any blockers", "is anything blocked", "status"])
def test_a_status_question_that_arrives_as_free_words_is_answered_with_the_status(skill, said):
    skill.connected = ["laptop", "tablet"]
    reply = skill.handler(answer_event(said), None)
    assert spoken(reply) == STATUS_SENTENCE and ("release_status" in [n for n, _ in skill.calls])


def test_a_sentence_that_is_not_a_command_still_goes_to_the_model_path(skill, monkeypatch):
    heard = []
    monkeypatch.setattr(skill.orchestrator, "run_turn", lambda *a, **k: heard.append(a[0]) or skill.orchestrator.Reply("ok"))
    skill.handler(answer_event("allow the first one on pull request 2 because it is only test data"), None)
    assert heard == ["allow the first one on pull request 2 because it is only test data"]
