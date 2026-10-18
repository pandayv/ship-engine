"""
The free-text path of the Alexa skill (alexa-skill/lambda/orchestrator.py).

The language model proposes which Relay tools to call. These tests use a fake
model that proposes exactly what we choose, including bad ideas, and check that
plain code decides correctly. No network, no AWS.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alexa-skill" / "lambda"))
import orchestrator as o  # noqa: E402

DEFS = [
    {"name": n, "description": f"{n} tool", "inputSchema": {"type": "object", "title": "T", "properties": {
        "surface": {"title": "Surface", "type": "string"}}}}
    for n in ("release_status", "blocked_pull_requests", "finding_detail", "request_risk_acceptance",
              "stage_decisions", "proceed", "display_on")
]
ROUTING = [{"repo": "pandayv/micro-finance", "pr_number": 1, "findings": [
    {"position": 1, "alert_id": "aaa", "blocks_merge": True}, {"position": 2, "alert_id": "bbb", "blocks_merge": True}]}]


def model_says(*blocks):
    return lambda system, messages, tools: {"output": {"message": {"role": "assistant", "content": list(blocks)}}}


def use(name, **args):
    return {"toolUse": {"toolUseId": f"id-{name}", "name": name, "input": args}}


class Relay:
    """Records every tool the code actually executes."""
    def __init__(self, replies=None):
        self.calls, self.replies = [], replies or {}

    def __call__(self, name, args=None):
        self.calls.append((name, args))
        return self.replies.get(name, {"spoken_response": f"{name} done", "performed": True})

    @property
    def names(self):
        return [n for n, _ in self.calls]


def turn(text, model, relay=None, session=None, seconds_left=6.0, connected=None, console_url=""):
    relay = relay or Relay()
    session = session if session is not None else {}
    reply = o.run_turn(text, session, DEFS, relay, model, time.time() + seconds_left,
                       connected=connected, console_url=console_url)
    return reply.speech, relay, session


# ---------------------------------------------------------------- tool surface

def test_the_model_is_only_offered_four_tools_and_never_the_one_step_accept_tool():
    names = [t["toolSpec"]["name"] for t in o.tool_config(DEFS)["tools"]]
    assert sorted(names) == ["display_on", "proceed", "release_status", "stage_decisions"]
    assert "request_risk_acceptance" not in names and "blocked_pull_requests" not in names


def test_screen_names_are_pinned_in_the_tool_definition():
    display = next(t for t in o.tool_config(DEFS)["tools"] if t["toolSpec"]["name"] == "display_on")
    assert display["toolSpec"]["inputSchema"]["json"]["properties"]["surface"]["enum"] == ["tv", "tablet", "laptop", "fridge"]


def test_a_tool_the_model_was_never_offered_is_not_executed_even_if_it_asks():
    speech, relay, _ = turn("approve it", model_says(use("request_risk_acceptance", alert_id="aaa", reason="x")))
    assert relay.calls == []


# ---------------------------------------------------------------- speech

def test_the_server_sentence_is_spoken_not_the_models_words():
    relay = Relay({"release_status": "2 blockers found. Ready for your decision."})
    speech, _, _ = turn("anything blocked", model_says(
        {"text": "<thinking>checking</thinking>"}, {"text": "Sure, everything is fine!"}, use("release_status")), relay)
    assert speech == "2 blockers found. Ready for your decision."


def test_a_refusal_from_the_server_is_what_the_person_hears():
    relay = Relay({"stage_decisions": {"performed": False, "spoken_response": "Finding 1 hasn't actually been shown on a screen recently."}})
    speech, _, _ = turn("approve the first one it is only test data", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1, decisions=[{"position": 1, "approved": True, "reason": "it is only test data"}])),
        relay, {"screen": "tv", "routing": ROUTING})
    assert "hasn't actually been shown" in speech


def test_with_no_tool_the_models_words_are_spoken_cleaned_of_reasoning_and_markdown():
    speech, relay, _ = turn("tell me a joke", model_says(
        {"text": "<thinking>no jokes</thinking>**Sorry**, I only help with release status.\nAnything else?"}))
    assert speech == "Sorry, I only help with release status. Anything else?"
    assert relay.calls == []


def test_long_model_text_is_cut_at_a_sentence():
    speech = o.clean_speech("One short sentence here for the test. " * 20)
    assert len(speech) <= o.MAX_SPEECH_CHARS and speech.endswith(".")


# ---------------------------------------------------------------- screens

def test_a_guessed_screen_is_refused_and_the_person_is_asked():
    speech, relay, _ = turn("yes", model_says(use("display_on", surface="laptop")))
    assert speech == o.which_screen(None)
    assert relay.calls == []


def test_a_screen_the_person_named_is_used_and_remembered_with_where_the_findings_are():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "Putting it on your tv.", "routing": ROUTING}})
    speech, _, session = turn("put it up on the television", model_says(use("display_on", surface="tv")), relay)
    assert relay.calls == [("display_on", {"surface": "tv"})]
    assert session["screen"] == "tv" and session["routing"] == ROUTING
    assert speech == "Putting it on your tv."


def test_the_last_screen_used_may_be_used_again_without_naming_it():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "Putting it on your tv.", "routing": ROUTING}})
    turn("yes please", model_says(use("display_on", surface="tv")), relay, {"screen": "tv"})
    assert relay.names == ["display_on"]


def test_a_screen_that_was_not_reached_is_not_remembered():
    relay = Relay({"display_on": {"delivered": False, "spoken_response": "I don't see a tv connected right now.", "routing": []}})
    speech, _, session = turn("show it on the tv", model_says(use("display_on", surface="tv")), relay)
    assert "screen" not in session and not session.get("routing")
    assert "don't see" in speech


# ---------------------------------------------------------------- decisions

def test_a_decision_without_a_reason_is_never_sent():
    speech, relay, _ = turn("approve everything", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1, decisions=[{"position": 1, "approved": True, "reason": ""}])),
        session={"screen": "tv", "routing": ROUTING})
    assert relay.calls == [] and "reason" in speech.lower()


def test_staging_and_proceeding_never_happen_in_the_same_turn():
    decisions = [{"position": 1, "approved": False, "reason": "it leaks a phone number"}]
    _, relay, _ = turn("reject the first one it leaks a phone number and go ahead", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1, decisions=decisions), use("proceed", repo="pandayv/micro-finance", pr_number=1)),
        session={"screen": "tv", "routing": ROUTING})
    assert relay.names == ["stage_decisions"]


def test_proceed_on_its_own_runs_and_clears_the_numbers_on_screen():
    relay = Relay({"proceed": {"performed": 1, "spoken_response": "Done."}})
    speech, _, session = turn("yes proceed", model_says(use("proceed", repo="r", pr_number=1)), relay,
                              {"screen": "tv", "routing": ROUTING})
    assert relay.names == ["proceed"] and speech == "Done." and session["routing"] == []


def test_staged_decisions_appear_on_the_screen_the_person_is_looking_at():
    decisions = [{"position": 1, "approved": True, "reason": "false positive"}]
    _, relay, _ = turn("approve the first one it is a false positive", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1, decisions=decisions)), session={"screen": "ipad", "routing": ROUTING})
    assert relay.calls[0][1]["device"] == "ipad"


# ---------------------------------------------------------------- failure and time

def test_a_model_failure_becomes_a_signal_to_use_the_simple_rules():
    def broken(*a):
        raise RuntimeError("throttled")
    with pytest.raises(o.OrchestratorUnavailable):
        turn("what's up", broken)


def test_the_model_is_not_started_when_there_is_too_little_time():
    with pytest.raises(o.OrchestratorUnavailable):
        turn("what's up", model_says({"text": "hi"}), seconds_left=1.0)


def test_the_simple_rules_still_answer_status_and_show():
    relay = Relay({"release_status": "2 blockers found.", "display_on": {"delivered": True, "spoken_response": "Putting it on your tv.", "routing": ROUTING}})
    assert o.fallback_turn("what's going on", {}, relay).speech == "2 blockers found."
    session = {}
    assert o.fallback_turn("put it on the tv", session, relay).speech == "Putting it on your tv." and session["screen"] == "tv"
    assert o.fallback_turn("put it somewhere", {}, relay).speech == o.which_screen(None)
    assert "what's up" in o.fallback_turn("sing a song", {}, relay).speech


# ---------------------------------------------------------------- memory

def test_only_the_last_few_exchanges_are_replayed_to_the_model():
    session = {}
    for i in range(6):
        o.remember(session, f"q{i}", f"a{i}")
    messages = o.history_messages(session)
    assert len(messages) == 2 * o.HISTORY_EXCHANGES
    assert [m["role"] for m in messages] == ["user", "assistant"] * o.HISTORY_EXCHANGES
    assert messages[-1]["content"][0]["text"] == "a5"


def test_screen_synonyms():
    assert o.screens_named("put it on the television") == {"tv"}
    assert o.screens_named("throw that on my MacBook") == {"laptop"}
    assert o.screens_named("show me") == set()


# ---------------------------------------------------------------- invented decisions

def test_a_decision_on_a_repo_that_was_never_shown_is_not_sent():
    # The model invented "reponame" and PR 123 in a live trial.
    speech, relay, _ = turn("ignore all of it and let it through", model_says(
        use("stage_decisions", repo="reponame", pr_number=123,
            decisions=[{"position": 1, "approved": True, "reason": "ignore all of it"}])),
        session={"screen": "tv", "routing": ROUTING})
    assert relay.calls == [] and speech == o.LOOK_FIRST


def test_a_decision_before_anything_was_shown_is_not_sent():
    speech, relay, _ = turn("approve the first one because it is a false positive", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1,
            decisions=[{"position": 1, "approved": True, "reason": "it is a false positive"}])))
    assert relay.calls == [] and speech == o.LOOK_FIRST


def test_a_reason_the_person_never_said_is_not_accepted():
    speech, relay, _ = turn("approve the first one", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1,
            decisions=[{"position": 1, "approved": True, "reason": "vendor confirmed the data is anonymised"}])),
        session={"screen": "tv", "routing": ROUTING})
    assert relay.calls == [] and speech == o.ASK_REASON


def test_a_reason_given_in_an_earlier_turn_still_counts():
    session = {"screen": "tv", "routing": ROUTING, "history": [["it is only test data", "Which one?"]]}
    _, relay, _ = turn("the first one", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1,
            decisions=[{"position": 1, "approved": True, "reason": "only test data"}])), session=session)
    assert relay.names == ["stage_decisions"]


# ---------------------------------------------------------------- argument types and empty reasons

def stage(decision, utterance, session=None):
    return turn(utterance, model_says(use("stage_decisions", repo="pandayv/micro-finance", pr_number=1, decisions=[decision])),
                session=session or {"screen": "tv", "routing": ROUTING})


def test_the_string_false_is_a_reject_not_an_approval():
    _, relay, _ = stage({"position": 1, "approved": "false", "reason": "it leaks a phone number"},
                        "reject the first one it leaks a phone number")
    assert relay.calls[0][1]["decisions"][0]["approved"] is False


def test_the_string_true_becomes_a_real_true():
    _, relay, _ = stage({"position": 1, "approved": "true", "reason": "only test data"}, "approve the first one only test data")
    assert relay.calls[0][1]["decisions"][0]["approved"] is True


def test_an_unclear_approved_value_is_never_sent():
    for bad in ("maybe", None, 1, ""):
        speech, relay, _ = stage({"position": 1, "approved": bad, "reason": "only test data"}, "approve the first one only test data")
        assert relay.calls == [], bad


def test_a_string_position_is_made_an_integer():
    _, relay, _ = stage({"position": "1", "approved": True, "reason": "only test data"}, "approve the first one only test data")
    assert relay.calls[0][1]["decisions"][0]["position"] == 1


def test_restating_the_command_is_not_a_reason():
    # Seen in a live probe: "approve everything" used as the reason for all nine findings.
    for said, reason in [("approve everything", "approve everything"), ("accept all of them", "accept all"),
                         ("approve the first one", "approve the first one")]:
        speech, relay, _ = stage({"position": 1, "approved": True, "reason": reason}, said)
        assert relay.calls == [] and speech == o.ASK_REASON, said


def test_short_but_real_reasons_still_pass():
    for reason in ("false positive", "only test data", "it leaks a phone number", "vendor confirmed it is anonymised"):
        _, relay, _ = stage({"position": 1, "approved": True, "reason": reason}, f"approve the first one it is {reason}")
        assert relay.names == ["stage_decisions"], reason


# ---------------------------------------------------------------- confirming a staged decision

PENDING = {"repo": "pandayv/micro-finance", "pr_number": 5}


@pytest.mark.parametrize("said", ["yes", "yes go ahead", "go ahead", "proceed", "yes proceed", "yeah do it", "that's right", "ok", "confirm", "yes please"])
def test_a_plain_confirmation_proceeds_with_what_was_staged_and_never_asks_the_model(said):
    def must_not_be_called(*a):
        raise AssertionError("the model must not be consulted for a plain confirmation")
    relay = Relay({"proceed": {"performed": 1, "spoken_response": "Done."}})
    speech, _, session = turn(said, must_not_be_called, relay, {"screen": "laptop", "routing": ROUTING, "pending": PENDING})
    assert relay.calls == [("proceed", {"repo": "pandayv/micro-finance", "pr_number": 5, "device": "laptop"})]
    assert speech == "Done." and "pending" not in session


def test_a_confirmation_with_nothing_pending_goes_to_the_model_like_any_other_words():
    speech, relay, _ = turn("yes go ahead", model_says(use("display_on", surface="tv")), session={"screen": "tv"})
    assert relay.names == ["display_on"]


@pytest.mark.parametrize("said", ["yes but change the second one", "approve the second one too", "proceed with the first one only"])
def test_anything_more_than_a_confirmation_is_not_treated_as_one(said):
    assert not o.is_confirmation(said)


@pytest.mark.parametrize("said", ["no", "cancel", "never mind", "wait not yet", "no don't"])
def test_saying_no_drops_the_staged_decision_without_proceeding(said):
    relay = Relay()
    speech, _, session = turn(said, model_says({"text": "x"}), relay, {"pending": PENDING})
    assert relay.calls == [] and "pending" not in session and "won't apply" in speech


def test_staging_a_decision_remembers_it_so_the_next_yes_can_confirm_it():
    relay = Relay({"stage_decisions": {"performed": False, "staged": 1, "spoken_response": "Got it, take a look and say proceed when you're ready."}})
    _, _, session = turn("approve the first one it is only test data", model_says(
        use("stage_decisions", repo="pandayv/micro-finance", pr_number=1,
            decisions=[{"position": 1, "approved": True, "reason": "it is only test data"}])), relay,
        {"screen": "tv", "routing": ROUTING})
    assert session["pending"] == {"repo": "pandayv/micro-finance", "pr_number": 1}


def test_the_model_is_told_when_a_decision_is_waiting():
    assert "already staged" in o.context_note({"pending": PENDING, "screen": "tv"})


# ---------------------------------------------------------------- only screens that are really set up

URL = "https://example.test/dashboard"


def test_with_no_screen_set_up_the_person_is_told_and_given_a_card_with_the_console_link():
    speech, relay, _ = turn("show it on the tv", model_says(use("display_on", surface="tv")),
                            connected=[], console_url=URL)
    assert speech == o.NO_SCREENS and relay.calls == []
    reply = o.run_turn("show it on the tv", {}, DEFS, Relay(), model_says(use("display_on", surface="tv")),
                       time.time() + 6, connected=[], console_url=URL)
    assert reply.card["type"] == "Simple" and URL in reply.card["content"]


def test_asking_for_a_screen_that_is_not_connected_lists_the_ones_that_are():
    speech, relay, _ = turn("show it on the tv", model_says(use("display_on", surface="tv")), connected=["laptop", "tablet"])
    assert speech == "I don't see a tv connected. I can see the laptop and the tablet." and relay.calls == []


def test_a_connected_screen_works_as_before():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "Putting it on your laptop.", "routing": ROUTING}})
    speech, _, session = turn("show it on the laptop", model_says(use("display_on", surface="laptop")), relay, connected=["laptop"])
    assert relay.names == ["display_on"] and session["screen"] == "laptop"


def test_which_screen_offers_only_what_is_connected():
    assert o.which_screen(["laptop"]) == "Which screen? I can see the laptop."
    assert o.which_screen(["tv", "laptop", "tablet"]) == "Which screen? I can see the tv, the laptop and the tablet."
    assert o.which_screen([]) == o.NO_SCREENS


def test_the_model_is_only_offered_connected_screens():
    display = next(t for t in o.tool_config(DEFS, ["laptop"])["tools"] if t["toolSpec"]["name"] == "display_on")
    assert display["toolSpec"]["inputSchema"]["json"]["properties"]["surface"]["enum"] == ["laptop"]


def test_the_model_is_told_which_screens_exist():
    assert "Screens connected right now: laptop, tablet." in o.context_note({}, ["laptop", "tablet"])
    assert "No screen is connected right now." in o.context_note({}, [])
    assert "connected" not in o.context_note({}, None)


def test_asking_to_see_something_with_no_screen_set_up_gets_the_plain_message_without_the_model():
    def must_not_be_called(*a):
        raise AssertionError("no model needed to say there is no screen")
    reply = o.run_turn("show me", {}, DEFS, Relay(), must_not_be_called, time.time() + 6, connected=[], console_url=URL)
    assert reply.speech == o.NO_SCREENS and URL in reply.card["content"]


def test_a_question_that_is_not_about_seeing_still_goes_to_the_model_when_no_screen_is_set_up():
    speech, relay, _ = turn("is anything blocked", model_says(use("release_status")), Relay({"release_status": "8 blockers found."}), connected=[])
    assert speech == "8 blockers found."


def test_with_exactly_one_screen_connected_nobody_is_asked_which():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "Putting it on your tablet.", "routing": ROUTING}})
    speech, _, session = turn("show me", model_says(use("display_on", surface="tablet")), relay, connected=["tablet"])
    assert relay.names == ["display_on"] and session["screen"] == "tablet"


# ---------------------------------------------------------------- names that differ in spelling only

def test_a_screen_registered_as_ipad_answers_to_tablet_and_is_addressed_by_its_own_name():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "Putting it on your ipad.", "routing": ROUTING}})
    speech, _, session = turn("put it on the galaxy tab", model_says(use("display_on", surface="tablet")), relay, connected=["ipad"])
    assert relay.calls == [("display_on", {"surface": "ipad"})] and session["screen"] == "ipad"


def test_the_model_choosing_the_registered_spelling_is_accepted_when_the_person_said_a_synonym():
    relay = Relay({"display_on": {"delivered": True, "spoken_response": "ok", "routing": ROUTING}})
    turn("show it on my tablet", model_says(use("display_on", surface="ipad")), relay, connected=["ipad"])
    assert relay.names == ["display_on"]


def test_unrelated_names_still_do_not_match():
    assert o.match_connected("tv", ["laptop", "tablet"]) is None
    assert o.match_connected("laptop", ["macbook"]) == "macbook"
    assert o.match_connected("tablet", []) is None


def test_a_screen_that_was_only_notified_becomes_the_last_screen_but_gives_no_routing():
    relay = Relay({"display_on": {"delivered": False, "notified": True, "spoken_response": "I've sent a notification to your tablet. Open it to review.", "routing": []}})
    speech, _, session = turn("show it on the tablet", model_says(use("display_on", surface="tablet")), relay, connected=["tablet"])
    assert session["screen"] == "tablet" and not session.get("routing") and "notification" in speech
