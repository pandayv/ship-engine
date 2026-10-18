"""
The Alexa+ simulator page (docs/alexa-simulator.html) against a scripted fake Relay, run under Node.
It is an MCP client of the real, unauthenticated Relay, so the only thing between it and a decision is
what Relay itself enforces; these tests check that the page does not fake one and does not mark
findings as seen when no screen received them.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

NODE = shutil.which("node")
RUNNER = Path(__file__).resolve().parent / "node" / "simulator_runner.js"
PAGE = Path(__file__).resolve().parent.parent / "docs" / "alexa-simulator.html"
CLIENT = Path(__file__).resolve().parent.parent / "docs" / "ship-mcp-client.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PAYLOAD = {"pull_requests": [{"repo": "a/b", "pr_number": 1, "findings": [
    {"position": 1, "alert_id": "x1", "file": "f.py", "what_is_wrong": "w", "citation": "c", "blocks_merge": True}]}], "held": []}


def run(tools, *actions):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"tools": tools, "actions": list(actions)}, handle)
    done = subprocess.run([NODE, str(RUNNER), handle.name], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def names(out):
    return [c["name"] for c in out["calls"]]


BASE = {"release_status": "2 blockers found. Ready for your decision. Want it on the laptop or the tablet?",
        "connected_screens": {"screens": ["laptop", "tablet"]}, "blocked_pull_requests": PAYLOAD,
        "display_on": {"delivered": True, "spoken_response": "Putting it on your laptop."}}


def test_the_screen_choices_are_the_ones_relay_says_are_set_up():
    out = run(BASE, {"ask": "Alexa, what's up?"})
    assert out["offered"] == ["laptop", "tablet", "here"]
    html = PAGE.read_text()
    for hard_coded in ('data-surface="tv"', 'data-surface="fridge"', "📺", "🧊"):
        assert hard_coded not in html


def test_with_no_screen_set_up_only_here_is_offered_and_it_says_so():
    out = run({**BASE, "connected_screens": {"screens": []}}, {"ask": "what's up"})
    assert out["offered"] == ["here"] and any("No screen is set up" in t for t in out["transcript"])


def test_a_clear_status_offers_nothing():
    out = run({**BASE, "release_status": "No blocking issues. Good to move ahead."}, {"ask": "what's up"})
    assert out["offered"] == [] and out["deviceStatus"] == "All clear" and out["ring"] is False


def test_an_unreadable_status_is_not_shown_as_all_clear():
    out = run({**BASE, "release_status": "I can't check right now. Open the review console."}, {"ask": "what's up"})
    assert "Couldn't read the status" in out["deviceStatus"] and out["ring"] is False and out["offered"] == []


def test_findings_are_not_fetched_for_a_screen_that_did_not_receive_them():
    # Fetching them through blocked_pull_requests would record them as seen although nothing was shown.
    out = run({**BASE, "display_on": {"delivered": False, "notified": False, "spoken_response": "I don't see a tablet connected right now."}},
              {"show": "tablet"})
    assert names(out) == ["display_on"] and out["screenBody"] == ""
    assert any("don't see a tablet" in t for t in out["transcript"])


def test_a_screen_that_received_them_is_not_asked_for_a_second_copy():
    out = run(BASE, {"show": "laptop"})
    assert names(out) == ["display_on"] and "Sent to the laptop" in out["deviceStatus"]


def test_showing_here_fetches_the_findings_because_this_page_is_the_screen():
    out = run({**BASE, "display_on": {"delivered": True, "spoken_response": "Showing it here."}}, {"show": "here"})
    assert names(out) == ["display_on", "blocked_pull_requests"] and "Pull request #1" in out["screenBody"]


@pytest.mark.parametrize("said", ["Alexa, allow it.", "Alexa, block it.", "Alexa, approve it.", "Alexa, reject the first one.",
                                  "accept it", "clear it"])
def test_a_decision_phrase_never_calls_a_decision_tool_and_explains_what_a_decision_needs(said):
    out = run(BASE, {"ask": said})
    assert out["calls"] == [] or all(not n.startswith(("request_risk", "stage_", "proceed")) for n in names(out))
    assert "release_status" not in names(out)           # it is a decision, not a status question
    assert any("reason in your own words" in t for t in out["transcript"]), said
    html = PAGE.read_text()
    assert '"demo"' not in html and "alert_id: \"demo\"" not in html
    assert "not a voice command" not in html


def test_the_preset_button_offers_allow_not_approve():
    html = PAGE.read_text()
    assert 'data-q="Alexa, allow it."' in html and '"Allow it."' in html
    assert "Alexa, approve it." not in html


def test_a_status_question_that_mentions_blockers_is_still_a_status_question():
    out = run(BASE, {"ask": "Alexa, are there any blockers?"})
    assert "release_status" in names(out)


def test_a_failing_tool_does_not_put_the_raw_error_on_the_page():
    out = run({**BASE, "release_status": {"__isError": True, "detail": "KeyError at server.py"}}, {"ask": "what's up"})
    text = " ".join(out["transcript"])
    assert "Couldn't reach Relay" in text and "KeyError" not in text and "server.py" not in text


def test_the_client_treats_is_error_as_a_failure():
    assert "result.isError" in CLIENT.read_text()
