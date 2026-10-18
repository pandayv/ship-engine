"""
The display page (docs/ship-display.html) run under Node with a browser stub: how it reacts to what
the gateway sends, including messages it should never choke on.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

NODE = shutil.which("node")
RUNNER = Path(__file__).resolve().parent / "node" / "display_runner.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FINDINGS = {"type": "findings", "pull_requests": [{"repo": "a/b", "pr_number": 1, "findings": [
    {"position": 1, "alert_id": "x1", "file": "f.py", "what_is_wrong": "w", "citation": "c", "blocks_merge": True}]}]}


def play(*steps):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump(list(steps), handle)
    done = subprocess.run([NODE, str(RUNNER), handle.name], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


OPEN = {"open": True}
REGISTERED = {"message": {"type": "registered", "registered": "laptop"}}


def test_a_page_that_connects_registers_itself_and_shows_it_in_words_and_colour():
    out = play(OPEN, REGISTERED)
    assert out["sent"][0] == {"action": "register", "device_name": "laptop"}
    assert "watching as <b>laptop</b>" in out["status"] and out["label"] == "Connected"


def test_it_registers_again_every_few_minutes_to_stay_inside_the_idle_timeout():
    out = play(OPEN, REGISTERED, {"tick": True})
    assert [m["action"] for m in out["sent"]].count("register") == 2
    assert out["keepaliveMs"] == [5 * 60 * 1000]


@pytest.mark.parametrize("raw", ["not json", "null", "42", "[]", "{}", '{"type": 7}', '{"type": "something-new"}',
                                 '{"pull_requests": "nope"}', '{"pull_requests": [null]}', '{"pull_requests": [{"findings": 5}]}'])
def test_a_malformed_or_unknown_message_never_throws_and_changes_nothing(raw):
    out = play(OPEN, REGISTERED, {"raw": raw})
    assert out["threw"] is None and out["errors"] == []
    assert out["body"] == ""


def test_findings_in_the_old_shape_without_a_type_still_render():
    old = {k: v for k, v in FINDINGS.items() if k != "type"}
    out = play(OPEN, REGISTERED, {"message": old})
    assert "Pull request #1" in out["body"] and out["dot"] == "dot alert" and out["label"] == "Needs a decision"


def test_a_refused_registration_is_shown_and_asks_again_instead_of_looping():
    out = play(OPEN, {"message": {"type": "error", "action": "register", "error": "A screen name of 1 to 30 letters or digits is required."}})
    assert "not connected" in out["status"] and out["label"] == "Problem"
    assert out["nameError"].startswith("A screen name") and out["nameboxHidden"] is False
    assert out["sockets"] == 1          # no reconnect loop


def test_findings_are_acknowledged_only_while_the_page_is_visible():
    out = play(OPEN, REGISTERED, {"visibility": "hidden"}, {"message": FINDINGS})
    assert [m for m in out["sent"] if m["action"] == "ack"] == []         # nobody can see it yet
    out = play(OPEN, REGISTERED, {"visibility": "hidden"}, {"message": FINDINGS}, {"visibility": "visible"})
    assert [m for m in out["sent"] if m["action"] == "ack"] == [{"action": "ack", "device_name": "laptop", "alert_ids": ["x1"]}]


def test_findings_shown_while_visible_are_acknowledged_at_once_and_only_once():
    out = play(OPEN, REGISTERED, {"message": FINDINGS}, {"visibility": "hidden"}, {"visibility": "visible"})
    assert len([m for m in out["sent"] if m["action"] == "ack"]) == 1


def test_the_header_comment_no_longer_claims_the_page_cannot_touch_the_gate():
    html = (Path(__file__).resolve().parent.parent / "docs" / "ship-display.html").read_text()
    assert "touch Gate" not in html
    assert "That is why the ack waits until the page is visible" in html


def test_the_status_line_is_announced_and_the_dot_has_a_text_label():
    html = (Path(__file__).resolve().parent.parent / "docs" / "ship-display.html").read_text()
    assert 'id="statusLine" role="status" aria-live="polite"' in html and 'id="stateLabel"' in html


def test_the_periodic_re_register_does_not_blank_the_findings_on_screen():
    out = play(OPEN, REGISTERED, {"message": FINDINGS}, {"tick": True}, REGISTERED)
    assert "Pull request #1" in out["body"] and out["dot"] == "dot alert"
