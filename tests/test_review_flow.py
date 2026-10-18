"""
The guided review (alexa-skill/lambda/review_flow.py): Alexa walks the person through the findings
they have seen, one at a time.

Pure logic: a fake Relay and a fake screen, no Alexa, no AWS. What matters:
  - natural answers are understood ("first one allow", "test data", "don't allow it", "skip");
  - a bare yes never decides anything;
  - each decision is staged on Relay as it is made and applied only on a plain yes at the end;
  - nothing loops: every question accepts what the person says, and a repeated miss offers a way out.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "alexa-skill" / "lambda"))
import review_flow as rf  # noqa: E402

ROUTING = [
    {"repo": "r", "pr_number": 1, "findings": [{"position": 1, "alert_id": "a1"}, {"position": 2, "alert_id": "a2"}]},
    {"repo": "r", "pr_number": 2, "findings": [{"position": 1, "alert_id": "b1"}]},
]


class Relay:
    def __init__(self, stage_reply=None):
        self.calls = []
        self.stage_reply = stage_reply

    def __call__(self, name, args=None):
        self.calls.append((name, args))
        if name == "stage_decisions":
            self.last_staged = len(args["decisions"])
            return self.stage_reply or {"staged": len(args["decisions"]), "spoken_response": "Got it."}
        if name == "proceed":
            n = getattr(self, "last_staged", 1)
            return {"performed": n, "spoken_response": "Done.", "of": n}
        return {"spoken_response": "Okay."}

    def named(self, name):
        return [a for n, a in self.calls if n == name]


def ctx_for(relay, routing=ROUTING, screens=("laptop", "tablet")):
    return SimpleNamespace(
        call_tool=relay, seen_routing=lambda: routing,
        show=lambda name: rf.Turn(f"Putting it on your {name}."),
        show_all=lambda: rf.Turn("Putting it on both."),
    )


def started(relay=None, routing=ROUTING):
    relay = relay or Relay()
    session = {"screen": "laptop"}
    turn = rf.begin(session, routing)
    return session, relay, turn


# ------------------------------------------------------------------ understanding


@pytest.mark.parametrize("said, action, reason", [
    ("allow", "allow", ""), ("allow it", "allow", ""), ("approve", "allow", ""), ("it's fine", "allow", ""),
    ("block", "block", ""), ("block it", "block", ""), ("reject", "block", ""), ("it's real", "block", ""),
    ("allow it because it is test data", "allow", "test data"),
    ("allow, it's only test data", "allow", "only test data"),
    ("test data", "allow", "test data"), ("false positive", "allow", "false positive"),
    ("that's fine, it's only test data", "allow", "only test data"),
    ("block it, it leaks the SSN", "block", "leaks the ssn"),
    ("the third one is a real leak", "block", "real leak"),
    ("first one is fine", "allow", ""), ("the 2nd one is fine because it is test data", "allow", "test data"),
    ("3rd 1 allow, it's a test fixture", "allow", "test fixture"), ("number 3 block it", "block", ""),
    ("don't allow it", "block", ""), ("do not block it", "allow", ""),
    ("skip", "skip", ""), ("next", "skip", ""), ("not sure", "skip", ""), ("leave it for now", "skip", ""),
    ("stop", "stop", ""), ("that's enough", "stop", ""), ("go back", "back", ""), ("undo", "back", ""),
    ("help", "help", ""), ("repeat that", "repeat", ""), ("which one", "repeat", ""),
])
def test_natural_answers_are_understood(said, action, reason):
    parsed = rf.parse_answer(said)
    assert parsed["action"] == action and parsed["reason"] == reason


@pytest.mark.parametrize("said", ["banana", "yes", "no", "allow and block it", "maybe", ""])
def test_an_answer_that_is_not_a_decision_is_not_read_as_one(said):
    assert rf.parse_answer(said)["action"] is None


def test_a_number_said_in_words_is_found_in_any_position():
    assert rf.parse_answer("first one allow")["number"] == 1
    assert rf.parse_answer("number 3 block it")["number"] == 3
    assert rf.parse_answer("block the second 1 because it leaks data")["number"] == 2
    assert rf.parse_answer("allow it")["number"] is None


def test_all_of_them_is_a_bulk_decision():
    parsed = rf.parse_answer("allow all because they are test fixtures")
    assert parsed["bulk"] and parsed["reason"] == "they are test fixtures"
    assert not rf.parse_answer("allow it because it is test data")["bulk"]


@pytest.mark.parametrize("said, expected", [
    ("yes", "yes"), ("yeah sure", "yes"), ("go ahead", "yes"), ("ready", "yes"), ("it's open", "yes"), ("let's start", "yes"),
    ("no", "no"), ("not now", "no"), ("not ready", "no"), ("no thanks", "no"), ("later", "no"),
    ("banana", None), ("", None),
])
def test_yes_and_no_are_understood_in_their_many_forms(said, expected):
    assert rf.parse_confirm(said) == expected


# ------------------------------------------------------------------ walking through


def test_it_starts_on_the_first_finding_and_asks_a_question_that_captures_any_answer():
    _, _, turn = started()
    assert turn.elicit
    assert turn.speech == "Pull request 1 has 2 findings on your screen. Number 1 on pull request 1. Allow, block, or skip?"


def test_a_decision_with_a_reason_in_one_sentence_is_staged_and_the_next_question_follows():
    session, relay, _ = started()
    turn = rf.answer(session, "allow it, it's only test data", ctx_for(relay))
    staged = relay.named("stage_decisions")
    assert staged == [{"repo": "r", "pr_number": 1, "device": "laptop", "decisions": [
        {"alert_id": "a1", "position": 1, "decision": "allow", "reason": "only test data"}]}]
    assert turn.speech == "Got it. Number 2. Allow, block, or skip?" and turn.elicit


def test_a_decision_without_a_reason_asks_for_one_and_says_what_a_reason_could_be():
    session, relay, _ = started()
    turn = rf.answer(session, "allow", ctx_for(relay))
    assert relay.named("stage_decisions") == []
    assert turn.speech == rf.ALLOW_EXAMPLE and turn.elicit
    turn = rf.answer(session, "because it is test data", ctx_for(relay))
    assert relay.named("stage_decisions")[0]["decisions"][0]["reason"] == "test data"
    assert turn.speech.startswith("Got it. Number 2")


def test_whatever_is_said_to_a_why_question_is_taken_as_the_reason():
    session, relay, _ = started()
    rf.answer(session, "block", ctx_for(relay))
    rf.answer(session, "the vendor sends that field to a model without masking it", ctx_for(relay))
    assert relay.named("stage_decisions")[0]["decisions"][0] == {
        "alert_id": "a1", "position": 1, "decision": "block",
        "reason": "vendor sends that field to a model without masking it"}


def test_the_whole_walk_through_stages_each_decision_and_applies_only_on_a_yes():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow, it's test data", ctx)
    turn = rf.answer(session, "block it because it leaks personal data", ctx)
    assert turn.speech == "Got it. That's all for pull request 1: 1 allowed, 1 blocked. Say yes to apply." and turn.elicit
    assert len(relay.named("stage_decisions")[-1]["decisions"]) == 2      # cumulative: both are staged
    assert relay.named("proceed") == []                                    # nothing applied yet
    turn = rf.answer(session, "yes", ctx)
    assert relay.named("proceed") == [{"repo": "r", "pr_number": 1, "device": "laptop"}]
    assert turn.speech.startswith("Done.") and "Want to go on to pull request 2?" in turn.speech


def test_after_a_pull_request_the_next_one_is_offered_and_a_yes_goes_on():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow all because they are test fixtures", ctx)
    rf.answer(session, "yes", ctx)
    turn = rf.answer(session, "yes", ctx)
    assert turn.speech.startswith("Pull request 2 has 1 finding on your screen.")


def test_saying_no_to_the_next_pull_request_ends_the_review():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow all because they are test fixtures", ctx)
    rf.answer(session, "yes", ctx)
    turn = rf.answer(session, "no", ctx)
    assert "flow" not in session and turn.speech == "Okay. They're still open." and not turn.elicit


def test_a_finding_named_by_number_is_decided_wherever_the_walk_through_is():
    session, relay, _ = started()
    rf.answer(session, "number 2 block because it leaks personal data", ctx_for(relay))
    assert relay.named("stage_decisions")[0]["decisions"][0]["alert_id"] == "a2"
    # and the walk-through continues with the finding that is still open
    assert session["flow"]["queue"][session["flow"]["i"]][0] == "a1"


def test_a_number_the_person_has_not_seen_is_not_decided():
    session, relay, _ = started()
    turn = rf.answer(session, "number 9 allow because it is test data", ctx_for(relay))
    assert relay.named("stage_decisions") == [] and "I don't have a number 9" in turn.speech


def test_all_of_them_decides_every_open_finding_on_the_pull_request_with_one_reason():
    session, relay, _ = started()
    turn = rf.answer(session, "allow all of them because they are test fixtures", ctx_for(relay))
    decisions = relay.named("stage_decisions")[-1]["decisions"]
    assert [d["alert_id"] for d in decisions] == ["a1", "a2"] and {d["reason"] for d in decisions} == {"they are test fixtures"}
    assert turn.speech == "Got it, all of them. That's all for pull request 1: 2 allowed. Say yes to apply."


def test_skipping_leaves_a_finding_undecided_and_never_stages_it():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    turn = rf.answer(session, "skip", ctx)
    assert relay.named("stage_decisions") == [] and turn.speech.startswith("Skipped. Number 2")
    turn = rf.answer(session, "next", ctx)
    assert turn.speech == "Skipped. You skipped all of pull request 1. Want to go on to pull request 2?"


def test_going_back_takes_the_last_decision_off_and_asks_about_it_again():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow, it's test data", ctx)
    turn = rf.answer(session, "go back", ctx)
    assert turn.speech == "Okay. Number 1 on pull request 1. Allow, block, or skip?"
    assert relay.named("cancel_pending")      # nothing left staged, so the screen stops showing it pending


def test_stopping_with_decisions_made_asks_whether_to_apply_them_and_no_drops_them():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow, it's test data", ctx)
    turn = rf.answer(session, "stop", ctx)
    assert "Say yes to apply them, or no to drop them." in turn.speech
    turn = rf.answer(session, "no", ctx)
    assert relay.named("proceed") == [] and relay.named("cancel_pending") and "dropped" in turn.speech


def test_stopping_before_deciding_anything_just_ends():
    session, relay, _ = started()
    turn = rf.answer(session, "stop", ctx_for(relay))
    assert "flow" not in session and not turn.elicit


# ------------------------------------------------------------------ no loops, no guessing


def test_a_bare_yes_does_not_decide_anything():
    session, relay, _ = started()
    turn = rf.answer(session, "yes", ctx_for(relay))
    assert relay.named("stage_decisions") == [] and "Allow, block, or skip?" in turn.speech and turn.elicit


def test_what_cannot_be_understood_is_asked_again_and_the_second_miss_offers_a_way_out():
    session, relay, _ = started()
    first = rf.answer(session, "purple monkey dishwasher", ctx_for(relay))
    second = rf.answer(session, "purple monkey dishwasher", ctx_for(relay))
    assert first.speech.startswith("I didn't catch that.") and "stop" not in first.speech
    assert "Say stop to leave." in second.speech
    assert relay.named("stage_decisions") == []


def test_a_one_word_reason_is_asked_about_once_and_then_accepted_so_there_is_no_loop():
    session, relay, _ = started()
    rf.answer(session, "allow", ctx_for(relay))
    again = rf.answer(session, "fine", ctx_for(relay))
    assert "a few words about why" in again.speech and relay.named("stage_decisions") == []
    done = rf.answer(session, "fine", ctx_for(relay))
    assert relay.named("stage_decisions")[0]["decisions"][0]["reason"] and done.speech.startswith("Got it.")


def test_changing_your_mind_while_giving_a_reason_changes_the_decision():
    session, relay, _ = started()
    rf.answer(session, "allow", ctx_for(relay))
    turn = rf.answer(session, "actually block it", ctx_for(relay))
    assert turn.speech == "What's the reason?"      # the examples were said once already
    rf.answer(session, "it exposes the account number", ctx_for(relay))
    assert relay.named("stage_decisions")[0]["decisions"][0]["decision"] == "block"


def test_when_relay_refuses_the_decision_the_review_ends_with_relays_own_words():
    relay = Relay(stage_reply={"staged": 0, "spoken_response": "Finding 1 hasn't actually been shown on a screen recently."})
    session, _, _ = started(relay)
    turn = rf.answer(session, "allow, it's test data", ctx_for(relay))
    assert "flow" not in session and "pending" not in session
    assert turn.speech == "Finding 1 hasn't actually been shown on a screen recently." and not turn.elicit


def test_repeating_the_question_after_alexa_missed_the_answer():
    session, _, _ = started()
    assert rf.repeat(session).speech.startswith("Sorry, I missed that. Number 1 on pull request 1.")
    assert rf.repeat({}) is None


def test_an_answer_to_the_apply_question_that_is_neither_yes_nor_no_is_asked_again():
    session, relay, _ = started()
    ctx = ctx_for(relay)
    rf.answer(session, "allow all because they are test fixtures", ctx)
    turn = rf.answer(session, "hmm purple", ctx)
    assert relay.named("proceed") == [] and "Say yes to apply" in turn.speech


# ------------------------------------------------------------------ before the walk-through


def test_nothing_seen_means_nothing_to_walk_through():
    session = {}
    turn = rf.begin(session, [])
    assert not turn.elicit and "Say show it on a screen first." in turn.speech


def test_after_a_screen_shows_it_alexa_asks_whether_to_go_through_them():
    session = {}
    turn = rf.offer_after_show(session, "Putting it on your laptop.", True, False)
    assert turn.speech == "Putting it on your laptop. Want to go through them?" and turn.elicit
    assert rf.answer(session, "yeah", ctx_for(Relay())).speech.startswith("Pull request 1 has 2 findings")


def test_saying_no_to_the_offer_leaves_it_for_later():
    session = {}
    rf.offer_after_show(session, "Putting it on your laptop.", True, False)
    turn = rf.answer(session, "not now", ctx_for(Relay()))
    assert "flow" not in session and turn.speech == "Okay. Say start whenever you want to go through them."


def test_a_notification_waits_for_the_person_to_say_ready_and_checks_they_really_saw_it():
    session = {}
    turn = rf.offer_after_show(session, "I've sent a notification to your laptop. Open it to review.", False, True)
    assert turn.elicit and "Say ready" in turn.speech
    unseen = rf.answer(session, "ready", ctx_for(Relay(), routing=[]))
    assert "I don't see it open yet" in unseen.speech and session["flow"]["stage"] == rf.READY
    seen = rf.answer(session, "ready", ctx_for(Relay()))
    assert seen.speech.startswith("Pull request 1 has 2 findings")


def test_the_status_question_about_where_to_look_is_answered_by_naming_the_screen():
    session = {}
    rf.offer_screens(session, ["laptop", "tablet"], "9 blockers found across 2 PRs. Want it on the laptop or the tablet?")
    assert rf.answer(session, "laptop", ctx_for(Relay())).speech == "Putting it on your laptop."


@pytest.mark.parametrize("said", ["the tablet please", "tablet is fine", "i'll take the tablet", "my tablet"])
def test_the_screen_can_be_named_in_many_ways(said):
    session = {}
    rf.offer_screens(session, ["laptop", "tablet"], "Want it on the laptop or the tablet?")
    assert rf.answer(session, said, ctx_for(Relay())).speech == "Putting it on your tablet."


@pytest.mark.parametrize("said", ["both", "all of them", "everywhere", "both of them please"])
def test_both_screens_can_be_asked_for_in_many_ways(said):
    session = {}
    rf.offer_screens(session, ["laptop", "tablet"], "Want it on the laptop or the tablet?")
    assert rf.answer(session, said, ctx_for(Relay())).speech == "Putting it on both."


def test_a_yes_with_two_screens_asks_which_and_with_one_screen_just_shows_it():
    session = {}
    rf.offer_screens(session, ["laptop", "tablet"], "Want it on the laptop or the tablet?")
    assert rf.answer(session, "yes", ctx_for(Relay())).speech == "Which one, the laptop or the tablet?"
    session = {}
    rf.offer_screens(session, ["laptop"], "Want it on the laptop?")
    assert rf.answer(session, "yes", ctx_for(Relay())).speech == "Putting it on your laptop."


def test_a_no_to_the_screen_question_ends_it_politely():
    session = {}
    rf.offer_screens(session, ["laptop", "tablet"], "Want it on the laptop or the tablet?")
    turn = rf.answer(session, "no thanks", ctx_for(Relay()))
    assert "flow" not in session and not turn.elicit


def test_the_short_repeat_of_the_screen_question_names_at_most_two_screens():
    assert rf.offer_screens({}, ["laptop", "tablet"], "x").reprompt == "Want it on the laptop or the tablet?"
    assert rf.offer_screens({}, ["laptop"], "x").reprompt == "Want it on the laptop?"
    assert rf.offer_screens({}, ["laptop", "tablet", "tv"], "x").reprompt == "Want it on a screen?"


# ------------------------------------------------------------------ choosing among several pull requests

THREE = ROUTING + [{"repo": "r", "pr_number": 7, "findings": [{"position": 1, "alert_id": "c1"}]}]


def test_a_pull_request_can_be_named_in_many_ways():
    for said, number in [("pull request 7", 7), ("PR 7", 7), ("go to pull request number 7", 7), ("skip to pr 12", 12)]:
        assert rf.pr_named(said) == number
    assert rf.pr_named("the first one") is None and rf.pr_named("yes") is None


def test_yes_goes_on_to_the_next_pull_request_and_skip_moves_past_it():
    session = {"screen": "laptop"}
    rf.begin(session, THREE)
    ctx = ctx_for(Relay(), routing=THREE)
    rf.answer(session, "skip", ctx)
    rf.answer(session, "skip", ctx)              # PR 1 all skipped
    turn = rf.answer(session, "skip", ctx)        # at "Want to go on to pull request 2?" -> skip it
    assert turn.speech == "Skipped. Want to go on to pull request 7?" and session["flow"]["pr"] == 7


def test_saying_the_pull_request_number_jumps_straight_to_it():
    session = {"screen": "laptop"}
    rf.begin(session, THREE)
    ctx = ctx_for(Relay(), routing=THREE)
    rf.answer(session, "skip", ctx)
    rf.answer(session, "skip", ctx)
    turn = rf.answer(session, "pull request 7", ctx)
    assert turn.speech == "Pull request 7 has 1 finding on your screen. Number 1 on pull request 7. Allow, block, or skip?"


def test_the_offer_can_name_the_pull_request_to_start_with():
    session = {}
    rf.offer_after_show(session, "Putting it on your laptop.", True, False)
    turn = rf.answer(session, "yes, pull request 7", ctx_for(Relay(), routing=THREE))
    assert turn.speech.startswith("Pull request 7 has 1 finding") or turn.speech.startswith("Number 1 on pull request 7")


def test_a_pull_request_that_was_not_shown_is_not_started():
    session = {}
    rf.offer_after_show(session, "Putting it on your laptop.", True, False)
    turn = rf.answer(session, "pull request 99", ctx_for(Relay(), routing=THREE))
    assert "I don't have pull request 99" in turn.speech and "flow" in session


def test_skipping_the_last_pull_request_ends_the_review():
    session = {"screen": "laptop"}
    rf.begin(session, ROUTING)
    ctx = ctx_for(Relay())
    rf.answer(session, "skip", ctx)
    rf.answer(session, "skip", ctx)
    rf.answer(session, "skip", ctx)               # skip PR 2 (the only one left to offer)
    assert "flow" not in session


def test_a_pull_request_named_in_a_decision_wins_over_the_one_being_reviewed():
    session = {"screen": "laptop"}
    rf.begin(session, THREE)                                   # on pull request 1
    relay = Relay()
    turn = rf.answer(session, "allow the first one on pull request 7 because it is test data", ctx_for(relay, routing=THREE))
    staged = relay.named("stage_decisions")[0]
    assert staged["pr_number"] == 7 and staged["decisions"][0]["alert_id"] == "c1"
    assert turn.speech.startswith("Got it.") or "pull request 7" in turn.speech


def test_a_decision_for_another_pull_request_is_not_taken_once_this_one_is_partly_decided():
    session = {"screen": "laptop"}
    rf.begin(session, THREE)
    relay = Relay()
    rf.answer(session, "allow, it's test data", ctx_for(relay, routing=THREE))
    staged_before = len(relay.named("stage_decisions"))
    turn = rf.answer(session, "allow the first one on pull request 7 because it is test data", ctx_for(relay, routing=THREE))
    assert len(relay.named("stage_decisions")) == staged_before
    assert "We're on pull request 1" in turn.speech


def test_a_pull_request_that_has_not_been_shown_is_not_decided():
    session = {"screen": "laptop"}
    rf.begin(session, ROUTING)
    relay = Relay()
    turn = rf.answer(session, "allow the first one on pull request 99 because it is test data", ctx_for(relay))
    assert relay.named("stage_decisions") == [] and "I don't have pull request 99" in turn.speech


# ------------------------------------------------------------------ not repeating itself, and saying where it stands


def test_the_examples_for_a_reason_are_said_once_then_the_question_is_short_and_varied():
    session, relay, _ = started(routing=[{"repo": "r", "pr_number": 1, "findings": [
        {"position": n, "alert_id": f"a{n}"} for n in range(1, 6)]}])
    ctx = ctx_for(relay)
    asks = []
    for _ in range(4):
        asks.append(rf.answer(session, "allow", ctx).speech)
        rf.answer(session, "because it is test data", ctx)
    assert asks[0] == rf.ALLOW_EXAMPLE
    assert "For example" not in " ".join(asks[1:])
    assert asks[1:] == list(rf.SHORT_REASON_ASKS)


def test_the_pull_request_is_named_the_first_time_and_not_on_every_question_after():
    session, relay, first = started()
    assert "Number 1 on pull request 1." in first.speech
    second = rf.answer(session, "allow, it's test data", ctx_for(relay))
    assert second.speech == "Got it. Number 2. Allow, block, or skip?"
    # but a repeat names it in full again, in case the person lost track
    assert rf.repeat(session).speech.startswith("Sorry, I missed that. Number 2 on pull request 1.")


def test_after_applying_it_says_where_the_pull_request_stands():
    session, relay, _ = started(routing=[{"repo": "r", "pr_number": 1, "findings": [
        {"position": n, "alert_id": f"a{n}"} for n in (1, 2, 3)]}])
    ctx = ctx_for(relay)
    rf.answer(session, "allow, it's only test data", ctx)
    rf.answer(session, "skip", ctx)
    rf.answer(session, "block it, it leaks personal data", ctx)
    spoken = rf.answer(session, "yes", ctx).speech
    assert spoken == ("Done. 1 allowed, 1 blocked. Pull request 1 stays blocked until the code is fixed. "
                      "1 still needs a decision. That's everything you've looked at.")


def test_the_closing_sentence_names_allowed_blocked_and_what_still_needs_a_decision():
    flow = {"pr": 1, "decisions": [["a1", 1, "allow", "x"], ["a3", 3, "block", "y"]], "skipped": ["a2"]}
    assert rf._what_happened(flow) == ("Done. 1 allowed, 1 blocked. Pull request 1 stays blocked until the code is fixed. "
                                       "1 still needs a decision.")
    assert rf._what_happened({"pr": 2, "decisions": [["b1", 1, "allow", "x"]], "skipped": []}) == "Done. 1 allowed."
    assert rf._what_happened({"pr": 2, "decisions": [["b1", 1, "block", "x"], ["b2", 2, "block", "y"]], "skipped": ["b3", "b4"]}) == (
        "Done. 2 blocked. Pull request 2 stays blocked until the code is fixed. 2 still need a decision.")
