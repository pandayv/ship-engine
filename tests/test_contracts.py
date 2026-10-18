"""
The seams between components. Each piece has its own tests with fakes for its neighbors; these
tests connect the real pieces, so a change on one side that the other side does not know about
fails here instead of in front of a person.

  (a) the tools the model is offered are built from Relay's real tool definitions
  (b) what Relay says was shown -> what the skill remembers -> what the orchestrator sends back
  (c) the payload Relay builds -> what the display page renders
"""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import src.relay.server as relay
from src.relay.readmodel import ReleaseStatus, _group
from src.storage import pending_store, session_store
from src.storage.alert_store import Alert

SKILL_DIR = Path(__file__).resolve().parent.parent / "alexa-skill" / "lambda"
sys.path.insert(0, str(SKILL_DIR))
import orchestrator as o  # noqa: E402
import importlib.util  # noqa: E402


def relay_tool_defs():
    """The tools exactly as an MCP client receives them from tools/list."""
    tools = asyncio.run(relay.mcp.list_tools())
    return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]


# --- (a) the model's tools come from Relay's real definitions ---


def test_every_tool_the_model_may_use_exists_on_relay():
    on_relay = {d["name"] for d in relay_tool_defs()}
    assert set(o.MODEL_TOOLS) <= on_relay


def test_the_model_is_never_offered_a_decision_tool_other_than_stage_and_proceed():
    offered = {t["toolSpec"]["name"] for t in o.tool_config(relay_tool_defs())["tools"]}
    decision_tools = {"request_risk_acceptance", "stage_decisions", "proceed", "cancel_pending"}
    assert offered & decision_tools == {"stage_decisions", "proceed"}
    assert offered == set(o.MODEL_TOOLS)


def test_the_model_view_keeps_what_relay_requires():
    by_name = {d["name"]: d for d in relay_tool_defs()}
    for tool in o.tool_config(relay_tool_defs())["tools"]:
        spec = tool["toolSpec"]
        real = by_name[spec["name"]]["inputSchema"]
        shown = spec["inputSchema"]["json"]
        assert set(shown.get("required", [])) == set(real.get("required", [])) - {"device"}, spec["name"]
        for prop in shown["properties"]:
            assert prop in real["properties"], (spec["name"], prop)


def test_what_the_orchestrator_sends_for_a_decision_is_what_relays_schema_accepts():
    real = next(d for d in relay_tool_defs() if d["name"] == "stage_decisions")["inputSchema"]
    routing = [{"repo": "r", "pr_number": 1, "findings": [{"position": 1, "alert_id": "x", "blocks_merge": True}]}]
    sent = []
    use = {"toolUse": {"toolUseId": "1", "name": "stage_decisions", "input": {
        "repo": "r", "pr_number": 1, "decisions": [{"position": 1, "decision": "allow", "reason": "only test data"}]}}}
    o.run_turn("allow the first one only test data", {"screen": "tv", "routing": routing}, relay_tool_defs(),
               lambda name, args: sent.append((name, args)) or {"spoken_response": "ok"},
               lambda *a: {"output": {"message": {"content": [use]}}}, __import__("time").time() + 6)
    name, args = sent[0]
    assert set(args) <= set(real["properties"]) and set(real["required"]) <= set(args)
    assert set(args["decisions"][0]) == {"alert_id", "position", "decision", "reason"}
    assert args["decisions"][0]["decision"] in ("allow", "block")
    assert "approved" not in str(args)


def test_relays_model_facing_descriptions_stay_within_what_the_model_is_given():
    for tool in o.tool_config(relay_tool_defs())["tools"]:
        assert 0 < len(tool["toolSpec"]["description"]) <= 900


def test_relays_descriptions_say_what_the_tools_actually_do():
    descriptions = {d["name"]: d["description"] for d in relay_tool_defs()}
    assert "alert_id" in descriptions["stage_decisions"] and "stays blocked" in descriptions["stage_decisions"]
    assert "ONLY after the person has confirmed" in descriptions["proceed"]
    assert "outcomes" in descriptions["proceed"]


# --- (b) shown -> remembered -> sent back by id ---


class _SessionTable:
    def __init__(self):
        self.rows = {}

    def get_item(self, Key, **kw):
        row = self.rows.get(Key["alert_id"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[Item["alert_id"]] = Item


class _PendingTable(_SessionTable):
    def get_item(self, Key, **kw):
        row = self.rows.get(Key["pr_key"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[Item["pr_key"]] = Item

    def delete_item(self, Key):
        self.rows.pop(Key["pr_key"], None)


def finding(alert_id, score, pr=2):
    return Alert(alert_id=alert_id, repo="pandayv/micro-finance", pr_number=pr, file="loans/a.py", taxonomy_id="PIIE-001",
                 risk_score=score, plain_english_summary="s", citation="c", remediation_patch="p", status="frozen",
                 created_at="2026-09-24T01:00:00+00:00", severity="blocking", head_sha="sha")


@pytest.fixture
def world(monkeypatch):
    """Real Relay tools over in-memory stores, called by the real skill functions through call_tool."""
    alerts = [finding("a1", 9.0), finding("a2", 8.0)]
    sessions, pendings = _SessionTable(), _PendingTable()
    monkeypatch.setattr(session_store, "_table", lambda: sessions)
    monkeypatch.setattr(pending_store, "_table", lambda: pendings)
    monkeypatch.setattr("src.relay.readmodel.list_gate_alerts", lambda: alerts)
    monkeypatch.setattr(relay, "list_gate_alerts", lambda: alerts)
    monkeypatch.setattr(relay, "get_alert", lambda aid: next((a for a in alerts if a.alert_id == aid), None))
    resolved = []
    monkeypatch.setattr(relay, "resolve_alert", lambda alert_id, decision, reason, resolved_by: resolved.append((alert_id, decision)))
    monkeypatch.setattr(relay, "refresh_summary", lambda *a, **k: None)
    monkeypatch.setattr(relay, "sync_pr_check", lambda *a, **k: True)
    monkeypatch.setattr(relay, "post_pr_comment", lambda *a, **k: True)
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": ["c1"], "not_connected": False})
    monkeypatch.setattr(relay, "list_all_connections", lambda: [])
    monkeypatch.setattr(relay.push_store, "subscribed_device_names", lambda: set())

    spec = importlib.util.spec_from_file_location("ship_skill_for_contract", SKILL_DIR / "lambda_function.py")
    skill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(skill)

    def call_tool(name, args=None):
        """What the MCP round trip does: JSON in, the tool runs, JSON (or text) out."""
        out = getattr(relay, name)(**json.loads(json.dumps(args or {})))
        return json.loads(json.dumps(out)) if isinstance(out, dict) else out

    monkeypatch.setattr(skill, "call_tool", call_tool)
    return alerts, resolved, skill, call_tool


def say(text, call_tool, session, positions_and_reasons, decision="allow"):
    decisions = [{"position": p, "decision": decision, "reason": r} for p, r in positions_and_reasons]
    use = {"toolUse": {"toolUseId": "1", "name": "stage_decisions", "input": {
        "repo": "pandayv/micro-finance", "pr_number": 2, "decisions": decisions}}}
    return o.run_turn(text, session, relay_tool_defs(), call_tool,
                      lambda *a: {"output": {"message": {"content": [use]}}}, __import__("time").time() + 6, connected=["tv"])


def test_a_finding_that_arrives_after_the_screen_cannot_shift_what_the_second_one_means(world):
    alerts, resolved, skill, call_tool = world
    shown = call_tool("display_on", {"surface": "tv"})
    assert [f["alert_id"] for f in shown["routing"][0]["findings"]] == ["a1", "a2"]

    alerts.insert(0, finding("a0", 9.9))          # a riskier finding arrives: live order is now a0, a1, a2

    session = {"screen": "tv"}
    session["routing"] = skill.seen_routing()      # what the next turn does first
    assert {f["position"]: f["alert_id"] for f in session["routing"][0]["findings"]} == {1: "a1", 2: "a2"}

    reply = say("allow the second one because it is only test data", call_tool, session, [(2, "it is only test data")])
    assert session["pending"] == {"repo": "pandayv/micro-finance", "pr_number": 2}
    staged = pending_store.get_pending("pandayv/micro-finance", 2)
    assert [d["alert_id"] for d in staged] == ["a2"]                       # not a1, which is "second" in the live list

    o.run_turn("yes", session, relay_tool_defs(), call_tool, lambda *a: pytest.fail("no model for a yes"),
               __import__("time").time() + 6, connected=["tv"])
    assert resolved == [("a2", "allow")]


def test_a_finding_that_arrived_after_the_screen_cannot_be_decided_by_pointing_at_it(world):
    alerts, resolved, skill, call_tool = world
    call_tool("display_on", {"surface": "tv"})
    alerts.insert(0, finding("a0", 9.9))
    session = {"screen": "tv", "routing": skill.seen_routing()}

    reply = say("allow the third one because it is only test data", call_tool, session, [(3, "it is only test data")])

    assert "number 3" in reply.speech and resolved == [] and "pending" not in session


def test_a_finding_decided_elsewhere_between_the_screen_and_the_sentence_is_refused_honestly(world):
    alerts, resolved, skill, call_tool = world
    call_tool("display_on", {"surface": "tv"})
    session = {"screen": "tv", "routing": skill.seen_routing()}
    alerts.pop()                                    # a2 was decided in the console after the screen was drawn

    reply = say("allow the second one because it is only test data", call_tool, session, [(2, "it is only test data")])

    assert resolved == [] and pending_store.get_pending("pandayv/micro-finance", 2) is None
    assert "second" not in reply.speech and ("no longer" in reply.speech or "finding 2" in reply.speech)


def test_findings_shown_by_an_ack_from_a_closed_page_are_pointed_at_by_the_number_that_page_showed(world, monkeypatch):
    alerts, resolved, skill, call_tool = world
    import device_gateway_handler as gateway
    from src.storage import push_store

    class Table:
        def __init__(self, key):
            self.key, self.rows = key, {}
        def put_item(self, Item, **kw): self.rows[Item[self.key]] = dict(Item)
        def get_item(self, Key, **kw): return {"Item": self.rows[Key[self.key]]} if Key[self.key] in self.rows else {}
        def delete_item(self, Key): self.rows.pop(Key[self.key], None)
        def scan(self, **kw): return {"Items": list(self.rows.values())}

    pend = Table("device_name")
    monkeypatch.setattr(push_store, "_pending", lambda: pend)
    status = relay._findings_detail()
    payload = relay._findings_payload(status)
    push_store.save_pending("tablet", payload, [f.alert_id for p in status.pull_requests for f in p.findings])

    alerts.insert(0, finding("a0", 9.9))            # arrives before the person opens the notification
    event = {"requestContext": {"routeKey": "ack", "connectionId": "c", "domainName": "x", "stage": "p"},
             "body": json.dumps({"device_name": "tablet", "alert_ids": ["a1", "a2"]})}
    gateway.handler(event, None)

    findings = {f["alert_id"]: f for f in call_tool("findings_routing")["pull_requests"][0]["findings"]}
    assert findings["a2"]["shown_position"] == 2 and findings["a1"]["shown_position"] == 1
    assert findings["a0"]["seen"] is False


# --- (c) the payload Relay builds is what the page renders ---

NODE = shutil.which("node")


def render(payload, opts=None, label="SHIP release status"):
    runner = Path(__file__).resolve().parent / "node" / "render_runner.js"
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        json.dump({"payload": payload, "opts": opts, "label": label}, handle)
    done = subprocess.run([NODE, str(runner), handle.name], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def relay_payload(alerts, pending=None, held=None):
    status = ReleaseStatus(_group(alerts), held_only=held or [])
    original = pending_store.get_pending
    pending_store.get_pending = lambda repo, pr: pending.get((repo, pr)) if pending else None
    try:
        return json.loads(json.dumps(relay._findings_payload(status)))
    finally:
        pending_store.get_pending = original


def make(alert_id, pr, repo="a/one", score=9.0, severity="blocking", summary="Raw data reaches a model.", file="loans/a.py",
         created="2026-09-24T01:00:00+00:00"):
    a = finding(alert_id, score, pr)
    a.repo, a.severity, a.plain_english_summary, a.file, a.created_at = repo, severity, summary, file, created
    return a


@pytest.mark.skipif(NODE is None, reason="node is not installed")
class TestScreenRendersRelaysPayload:
    def test_numbering_restarts_in_each_pull_request_and_follows_relays_positions(self):
        payload = relay_payload([make("x1", 1, score=9), make("x2", 1, score=8), make("y1", 2, repo="b/two", score=9)])
        out = render(payload)
        html = out["html"]
        assert html.count('<section class="ship-pr">') == 2
        assert html.count('<td class="ship-num" data-label="Finding">1</td>') == 2
        assert html.count('<td class="ship-num" data-label="Finding">2</td>') == 1
        assert "Pull request #1" in html and "Pull request #2" in html

    def test_pull_requests_with_a_blocker_come_first_and_blockers_lead_within_one(self):
        # Oldest first is Relay's order, so the review-only PR (older) arrives first and must be moved after.
        payload = relay_payload([
            make("r1", 5, repo="old/review", severity="review", score=6.0, created="2026-09-01T00:00:00+00:00"),
            make("b1", 6, repo="new/blocked", created="2026-09-02T00:00:00+00:00"),
            make("b2", 6, repo="new/blocked", severity="review", score=6.0, created="2026-09-02T00:00:00+00:00"),
        ])
        assert [p["pr_number"] for p in payload["pull_requests"]] == [5, 6]            # Relay's own order
        html = render(payload)["html"]
        assert html.index("Pull request #6") < html.index("Pull request #5")            # the page puts the blocker first
        pr6 = html[html.index("Pull request #6"):html.index("Pull request #5")]
        assert pr6.index("Needs a decision") < pr6.index("Flagged")

    def test_the_header_counts_blocking_and_flagged(self):
        html = render(relay_payload([make("b1", 6), make("b2", 6, severity="review", score=6.0)]))["html"]
        assert "<b>1</b> blocking &middot; <b>1</b> flagged" in html

    def test_a_staged_block_shows_on_its_row(self):
        pending = {("a/one", 1): [{"alert_id": "x1", "decision": "block", "reason": "it leaks a phone number"}]}
        html = render(relay_payload([make("x1", 1), make("x2", 1, score=8)], pending=pending))["html"]
        assert 'Pending: block &mdash; it leaks a phone number' in html
        assert html.count("Pending:") == 1

    def test_a_staged_allow_shows_on_its_row(self):
        pending = {("a/one", 1): [{"alert_id": "x2", "decision": "allow", "reason": "it is only test data"}]}
        html = render(relay_payload([make("x1", 1), make("x2", 1, score=8)], pending=pending))["html"]
        assert 'Pending: allow &mdash; it is only test data' in html
        assert html.count("Pending:") == 1

    def test_the_payload_carries_decision_and_reason_for_a_staged_row(self):
        pending = {("a/one", 1): [{"alert_id": "x1", "decision": "allow", "reason": "it is only test data"}]}
        payload = relay_payload([make("x1", 1), make("x2", 1, score=8)], pending=pending)
        by_id = {f["alert_id"]: f for f in payload["pull_requests"][0]["findings"]}
        assert by_id["x1"]["pending"] == {"decision": "allow", "reason": "it is only test data"}
        assert by_id["x2"]["pending"] is None

    @pytest.mark.parametrize("bad", ["approve", "Allow", True, None, "", "approved"])
    def test_the_page_shows_no_pending_note_for_a_decision_that_is_not_allow_or_block(self, bad):
        payload = relay_payload([make("x1", 1)])
        payload["pull_requests"][0]["findings"][0]["pending"] = {"decision": bad, "reason": "whatever"}
        assert "Pending" not in render(payload)["html"]

    def test_a_pending_note_in_the_old_shape_shows_nothing(self):
        payload = relay_payload([make("x1", 1)])
        payload["pull_requests"][0]["findings"][0]["pending"] = {"approved": True, "reason": "whatever"}
        assert "Pending" not in render(payload)["html"]

    def test_an_open_blocking_finding_reads_needs_a_decision_so_it_is_not_mistaken_for_a_block(self):
        html = render(relay_payload([make("b1", 1)]))["html"]
        assert 'class="ship-chip blocking">Needs a decision</span>' in html
        assert "Merge blocked" not in html and "Flagged" not in html

    def test_a_non_blocking_finding_reads_flagged(self):
        html = render(relay_payload([make("r1", 1, severity="review", score=6.0)]))["html"]
        assert 'class="ship-chip review">Flagged</span>' in html
        assert "Needs a decision" not in html and "Merge blocked" not in html

    def test_the_chip_keeps_the_severity_classes_the_page_styles_depend_on(self):
        html = render(relay_payload([make("b1", 1), make("r1", 1, severity="review", score=6.0)]))["html"]
        assert '<tr class="blocking">' in html and '<tr class="review">' in html

    def test_the_hint_teaches_allow(self):
        html = render(relay_payload([make("x1", 1)]))["html"]
        assert "allow the first one on pull request 1 because it&rsquo;s test data" in html
        assert "approve" not in html.lower() and "reject" not in html.lower()

    def test_text_from_a_finding_cannot_inject_markup(self):
        nasty = make("x1", 1, summary='<img src=x onerror="alert(1)"> it\'s & "quoted"', file="a'b\"<c>.py")
        html = render(relay_payload([nasty]))["html"]
        assert "<img" not in html and "onerror=\"" not in html and "<c>" not in html
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; it&#39;s &amp; &quot;quoted&quot;" in html
        assert "a&#39;b&quot;&lt;c&gt;.py" in html

    def test_single_quotes_are_escaped(self):
        assert render(relay_payload([make("x1", 1)]))["escapes"] == "&lt;&amp;&quot;&#39;&gt;"

    def test_an_empty_payload_with_nothing_held_is_all_clear(self):
        out = render(relay_payload([]))
        assert "Nothing blocked" in out["html"]

    def test_blocked_findings_that_still_hold_the_merge_are_not_called_all_clear(self):
        from src.relay.readmodel import PullRequestFindings
        out = render(relay_payload([], held=[PullRequestFindings("a/one", 3, 0, 0, [], held=2)]))
        assert "Nothing blocked" not in out["html"] and "a/one #3 (2)" in out["html"]
        assert "Findings that were blocked still hold the merge until the code is fixed" in out["html"]
        assert "reject" not in out["html"].lower()

    def test_the_console_link_is_used_only_when_it_is_a_real_web_address(self):
        payload = relay_payload([make("x1", 1)])
        good = render(payload, {"consoleUrl": "https://ship.example/dashboard"})["appended"][0]["innerHTML"]
        assert 'href="https://ship.example/dashboard"' in good
        for bad in ('javascript:alert(1)', '" onmouseover="x', "/relative", "#", ""):
            html = render(payload, {"consoleUrl": bad})["appended"][0]["innerHTML"]
            assert 'href="#"' in html and "javascript:" not in html and "onmouseover" not in html, bad
        quoted = render(payload, {"consoleUrl": 'https://x.example/?a="b"&c=<d>'})["appended"][0]["innerHTML"]
        assert '"b"' not in quoted and "<d>" not in quoted

    def test_without_a_console_url_no_decision_box_is_added(self):
        assert render(relay_payload([make("x1", 1)]))["appended"] == []
