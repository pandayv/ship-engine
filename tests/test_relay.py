"""
Tests for Relay, SHIP's MCP server.

Two properties matter more than the rest and are asserted directly rather
than left to code review:

  1. Voice never narrates. A spoken response is one short sentence with no
     list, no citation and no code. This is enforced by the server, so a
     future prompt or a well-meaning change cannot quietly turn Relay
     into a narrator.
  2. No compliance finding can be decided BLIND. SHIP flags exactly this
     pattern in other people's code (TLGP-002 - an automated decision with
     no human checkpoint); doing it in its own interface would be the same
     violation. The rule is not "never by voice", it is "never without the
     finding having been shown on a screen recently, never without a reason
     in the person's own words, and never on a finding the person did not
     point at": see src/relay/server.py's module docstring. The tests for it
     assert what actually happens (a refusal changes nothing; an allow
     clears the PR's status; a block keeps it failing), not which tool
     names exist.
"""

import asyncio
from dataclasses import replace

import pytest

import src.relay.server as relay
from src.relay.modality import SPEECH_CHAR_BUDGET, ModalityViolation, Spoken
from src.relay.readmodel import ReleaseStatus, _group
from src.storage import pending_store, session_store
from src.storage.alert_store import (DECISION_ALLOW, DECISION_BLOCK, RESOLUTION_ALLOWED, RESOLUTION_BLOCKED, Alert,
                                     AlertNotFoundOrAlreadyResolved, is_held, is_open)
from src.storage.status_store import PullRequestCounts, ReleaseSummary, SummaryUnavailable


class _FakeSessionTable:
    """Real session_store logic against a fake DynamoDB table — exercises
    the actual mark_shown()/was_shown_recently() integration, not a
    mocked pass-through, matching this project's established testing
    convention (see test_session_store.py's own _FakeTable)."""

    def __init__(self):
        self.rows = {}

    def get_item(self, Key, **kw):
        row = self.rows.get(Key["alert_id"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[Item["alert_id"]] = Item


class _FakePendingTable:
    """Same approach as _FakeSessionTable — real pending_store logic
    against a fake table, not a pass-through mock."""

    def __init__(self):
        self.rows = {}

    def get_item(self, Key, **kw):
        row = self.rows.get(Key["pr_key"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[Item["pr_key"]] = Item

    def delete_item(self, Key):
        self.rows.pop(Key["pr_key"], None)


@pytest.fixture(autouse=True)
def session_table(monkeypatch):
    """Autouse, not opt-in: blocked_pull_requests/finding_detail/display_on all call
    session_store.mark_shown() unconditionally, and it fails SILENTLY by design (see
    session_store.py's docstring). A test that forgets to mock this doesn't fail, it quietly
    writes to the real ship-recent-reviews table instead. Making it automatic for every test in
    this file removes that failure mode instead of relying on every test author to remember it."""
    fake = _FakeSessionTable()
    monkeypatch.setattr(session_store, "_table", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def no_real_writeback(monkeypatch):
    """Autouse, same reasoning as session_table above. request_risk_acceptance's success path
    calls refresh_summary(), which writes to the real ship-status table when boto3 finds real
    credentials. GitHub write-back (post_pr_comment/sync_pr_check) is already safe without
    mocking, because _writeback_enabled() short-circuits with no GITHUB_TOKEN configured, but
    refresh_summary() has no such guard, by design (status_store.py's docstring: best-effort, not
    opt-out). Faked here rather than relying on every test that exercises a resolve path to
    remember it individually."""
    monkeypatch.setattr(relay, "refresh_summary", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def no_real_screens(monkeypatch):
    """Relay reads which screens exist (the status sentence's offer, connected_screens).
    Nothing connected unless a test says otherwise."""
    monkeypatch.setattr(relay, "list_all_connections", lambda: [])
    monkeypatch.setattr(relay.push_store, "subscribed_device_names", lambda: set())
    monkeypatch.setattr(relay.push_store, "subscriptions_for_device", lambda name: [])


@pytest.fixture(autouse=True)
def pending_table(monkeypatch):
    """Same reasoning as session_table — stage_decisions/proceed read and
    write pending_store for real; fake the table so tests never touch the
    real ship-pending-dispositions table."""
    fake = _FakePendingTable()
    monkeypatch.setattr(pending_store, "_table", lambda: fake)
    return fake


def _alert(repo="pandayv/micro-finance", pr=2, severity="blocking", score=9.0,
           created="2026-09-24T01:00:00+00:00", alert_id="a1"):
    return Alert(
        alert_id=alert_id, repo=repo, pr_number=pr, file="loans/ai_underwriting.py",
        taxonomy_id="PIIE-001", risk_score=score,
        plain_english_summary="The applicant's full profile reaches an external model unmasked.",
        citation="GDPR Art. 32(1)(a)", remediation_patch="# hash identifiers first",
        status="frozen", created_at=created, severity=severity, head_sha="abc123",
    )


def _status(alerts):
    return ReleaseStatus(_group(alerts))


def _summary_from(alerts) -> ReleaseSummary:
    """Builds the pre-computed summary a real write-path refresh would
    produce for these alerts, so tests exercise release_status() (which
    reads ONLY the stored summary, never the alerts table — see
    src/storage/status_store.py) against data consistent with the screen
    path's alerts, without either path touching real AWS."""
    from src.storage.alert_store import SEVERITY_BLOCKING

    buckets: dict[tuple[str, int], list] = {}
    for a in alerts:
        buckets.setdefault((a.repo, a.pr_number), []).append(a)
    counts = [
        PullRequestCounts(
            repo=repo, pr_number=pr,
            blocking=sum(1 for a in items if a.severity == SEVERITY_BLOCKING),
            review=sum(1 for a in items if a.severity != SEVERITY_BLOCKING),
            oldest_finding_at=min(a.created_at for a in items),
        )
        for (repo, pr), items in buckets.items()
    ]
    return ReleaseSummary(
        blocking=sum(c.blocking for c in counts),
        review=sum(c.review for c in counts),
        pull_requests=counts,
    )


@pytest.fixture
def one_blocking(monkeypatch):
    """Wires BOTH read paths to the same underlying alert, so voice
    (release_status, backed by read_summary()) and screen
    (blocked_pull_requests/finding_detail, backed by list_gate_alerts())
    see consistent data — and neither ever reaches real AWS."""
    alerts = [_alert()]
    monkeypatch.setattr("src.relay.readmodel.list_gate_alerts", lambda: alerts)
    monkeypatch.setattr(relay, "list_gate_alerts", lambda: alerts)
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: _summary_from(alerts))
    monkeypatch.setattr(relay, "get_alert", lambda aid: alerts[0] if aid == "a1" else None)
    return alerts


@pytest.fixture
def two_blocking(monkeypatch):
    """Two findings on the same PR, both blocking, same shape as
    one_blocking but for the multi-finding staging/proceed tools."""
    alerts = [_alert(alert_id="a1", score=9.0), _alert(alert_id="a2", score=8.0)]
    monkeypatch.setattr("src.relay.readmodel.list_gate_alerts", lambda: alerts)
    monkeypatch.setattr(relay, "list_gate_alerts", lambda: alerts)
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: _summary_from(alerts))
    monkeypatch.setattr(relay, "get_alert", lambda aid: next((a for a in alerts if a.alert_id == aid), None))
    return alerts


# --- property 1: voice never narrates ---


def test_spoken_status_fits_one_sentence(one_blocking):
    spoken = relay.release_status()
    assert len(spoken) <= SPEECH_CHAR_BUDGET + 40  # + the screen hint
    assert "\n" not in spoken


def test_spoken_status_never_leaks_screen_facts(one_blocking):
    # Citations, file paths, taxonomy ids and code are screen facts. If any
    # of them reach the spoken path, voice has started narrating.
    spoken = relay.release_status().lower()
    # A specific PR/repo label is also a screen fact now, not a voice one —
    # a raw "repo#42" reference is unpronounceable and meaningless to a
    # non-technical listener. Voice states counts only (how many, how many
    # places); which one is what "want it on a screen?" is for.
    for leak in ["gdpr", "art.", "piie", ".py", "hash identifiers", "risk score"]:
        assert leak not in spoken, f"spoken response leaked a screen fact: {leak!r}"


def test_a_long_list_cannot_be_spoken():
    # The budget is the enforcement: twelve findings cannot be compressed
    # into one sentence, so the attempt must fail loudly rather than
    # truncate into a half-read list.
    with pytest.raises(ModalityViolation):
        Spoken(", ".join(f"finding {i} in file_{i}.py" for i in range(12)))


def test_clean_state_is_reassuring_and_short(monkeypatch):
    # release_status() is the voice path and reads ONLY the pre-computed
    # summary (src/storage/status_store.py) — never list_gate_alerts, and
    # never real AWS. Mocking read_summary() is what actually isolates
    # this test; mocking list_gate_alerts() alone would silently fall
    # through to a real (and possibly non-empty) DynamoDB table.
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: ReleaseSummary())
    spoken = relay.release_status()
    assert "no blocking issues" in spoken.lower()
    assert len(spoken) <= SPEECH_CHAR_BUDGET


def test_release_status_never_reads_the_alerts_table_directly(monkeypatch):
    # release_status() must depend ONLY on the pre-computed summary. If it
    # ever falls back to list_active_alerts() or a live scan, this proves
    # it by making that path explode.
    def _must_not_be_called():
        raise AssertionError("release_status() must not read list_gate_alerts directly")

    monkeypatch.setattr("src.relay.readmodel.list_gate_alerts", lambda: _must_not_be_called())
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: ReleaseSummary())
    assert "no blocking issues" in relay.release_status().lower()


def test_screen_tools_are_marked_display_only(one_blocking):
    assert relay.blocked_pull_requests()["display_only"] is True
    assert relay.finding_detail("a1")["display_only"] is True


def test_screen_tools_carry_the_detail_voice_withholds(one_blocking):
    detail = relay.finding_detail("a1")
    assert detail["citation"] == "GDPR Art. 32(1)(a)"
    assert detail["blocks_merge"] is True
    assert "hash identifiers" in detail["suggested_remediation"]


# --- property 2: a finding cannot be decided blind ---
#
# These assert what happens, not which tool names exist: a refusal changes nothing, an
# allow clears the PR's commit status, a block keeps it failing.


async def mcp_tools():
    return await relay.mcp.list_tools()


class LiveDB:
    """Alerts held in memory with the real rules for what blocks a merge, so a test can
    decide a finding and then read back the status GitHub would be told."""

    def __init__(self, alerts):
        self.alerts = {a.alert_id: a for a in alerts}
        self.statuses = []      # every commit status posted: (sha, state, description)
        self.comments = []
        self.resolve_calls = []

    def get(self, alert_id):
        return self.alerts.get(alert_id)

    def resolve(self, alert_id, decision, reason, resolved_by):
        self.resolve_calls.append(alert_id)
        alert = self.alerts.get(alert_id)
        if alert is None or alert.status == "resolved":
            raise AlertNotFoundOrAlreadyResolved(alert_id)
        assert decision in (DECISION_ALLOW, DECISION_BLOCK), decision
        alert.status = "resolved"
        alert.resolution = RESOLUTION_ALLOWED if decision == DECISION_ALLOW else RESOLUTION_BLOCKED

    def gate(self):
        return [a for a in self.alerts.values() if is_open(a) or is_held(a)]


@pytest.fixture
def live(monkeypatch):
    """Two blocking findings, wired through the real status derivation. GitHub is faked at the
    HTTP layer: the PR's current head is "currenthead", the alert's stored sha is the stale "abc123"."""
    import src.api.github_writeback as wb

    db = LiveDB([_alert(alert_id="a1", score=9.0), _alert(alert_id="a2", score=8.0)])
    for a in db.alerts.values():
        a.fragment_key = f"frag-{a.alert_id}"

    class _Response:
        def __init__(self, body=None):
            self._body = body or {}

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(wb.requests, "get", lambda url, **k: _Response({"head": {"sha": "currenthead"}}))
    monkeypatch.setattr(wb.requests, "post", lambda url, json=None, **k: (
        db.statuses.append((url.rsplit("/", 1)[-1], json["state"], json["description"])) if "/statuses/" in url
        else db.comments.append(json["body"])) or _Response())
    monkeypatch.setattr("src.relay.readmodel.list_gate_alerts", db.gate)
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: _summary_from(
        [a for a in db.alerts.values() if is_open(a)]))
    monkeypatch.setattr(relay, "list_gate_alerts", db.gate)
    monkeypatch.setattr(relay, "get_alert", db.get)
    monkeypatch.setattr(relay, "resolve_alert", db.resolve)
    monkeypatch.setattr(wb, "list_alerts_for_pr", lambda repo, pr: db.gate())
    return db


def test_blind_acceptance_never_performs_it(one_blocking, session_table):
    # No screen tool was ever called for this alert: the exact "ignore it, go ahead" shape.
    result = relay.request_risk_acceptance("a1", reason="looks fine")
    assert result["performed"] == 0
    assert result["reason"] == "requires_attested_human_decision"


def test_an_unseen_finding_cannot_be_decided_by_any_path_and_nothing_changes(live):
    one = relay.request_risk_acceptance("a1", reason="it is only test data")
    staged = relay.stage_decisions("pandayv/micro-finance", 2, [
        {"alert_id": "a1", "decision": "allow", "reason": "it is only test data"}])
    assert one["performed"] == 0 and staged["performed"] == 0 and "staged" not in staged
    assert live.resolve_calls == [] and live.statuses == []
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_blind_refusal_is_short_enough_to_speak_and_names_the_real_gap(one_blocking, session_table):
    spoken = relay.request_risk_acceptance("a1")["spoken_response"]
    assert len(spoken) <= SPEECH_CHAR_BUDGET
    assert "screen" in spoken.lower()


def test_refusal_routes_to_a_surface_where_the_decision_can_be_made(one_blocking, session_table, monkeypatch):
    monkeypatch.setattr(relay, "CONSOLE_URL", "https://ship.example")
    assert relay.request_risk_acceptance("a1")["console_url"].startswith("https://ship.example/dashboard")


def test_reviewed_then_confirmed_with_a_reason_actually_performs_it(live):
    relay.finding_detail("a1")  # the actual review
    result = relay.request_risk_acceptance("a1", reason="known internal test fixture")

    assert result["performed"] == 1 and type(result["performed"]) is int
    assert live.alerts["a1"].resolution == "allowed"
    assert "known internal" not in result["spoken_response"]     # the reason is recorded, not echoed


def test_a_long_reason_is_refused_before_anything_is_written(live):
    relay.finding_detail("a1")
    result = relay.request_risk_acceptance("a1", reason="because " * 200)
    assert result["performed"] == 0 and live.resolve_calls == []
    assert len(result["spoken_response"]) <= SPEECH_CHAR_BUDGET


def test_reviewed_but_no_reason_still_refuses(live):
    relay.finding_detail("a1")
    result = relay.request_risk_acceptance("a1")  # no reason

    assert result["performed"] == 0 and live.resolve_calls == []
    assert "reason" in result["spoken_response"].lower()


def test_a_stale_review_past_the_window_does_not_unlock_it(live, session_table):
    relay.finding_detail("a1")
    # Simulate the review having happened outside the recent window,
    # rather than asserting against wall-clock timing.
    session_table.rows["a1"]["shown_at"] -= session_store.RECENT_WINDOW_SECONDS + 1

    result = relay.request_risk_acceptance("a1", reason="go ahead")

    assert result["performed"] == 0 and live.resolve_calls == []


def test_display_on_a_named_screen_also_counts_as_reviewed(live, monkeypatch):
    # Asking Alexa to push to the TV and then saying "override it" in the
    # same breath must work: the person asking is who'll plausibly say
    # that next, regardless of which physical screen it renders on.
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": ["tv1"], "not_connected": False})

    relay.display_on("tv")
    result = relay.request_risk_acceptance("a1", reason="reviewed on the tv")

    assert result["performed"] == 1


def test_hearing_only_release_status_does_not_count_as_reviewed(live):
    # release_status is voice-only and never names a specific finding: it
    # must not be mistaken for having shown anything.
    relay.release_status()  # never touches session_store at all
    result = relay.request_risk_acceptance("a1", reason="go ahead")

    assert result["performed"] == 0 and live.resolve_calls == []


def test_an_already_decided_finding_is_reported_not_decided_again(live):
    relay.finding_detail("a1")
    relay.request_risk_acceptance("a1", reason="known internal test fixture")
    again = relay.request_risk_acceptance("a1", reason="a different reason")
    assert again["performed"] == 0 and "already decided" in again["spoken_response"]
    assert live.resolve_calls == ["a1"]


# --- what a decision does to the pull request ---


def test_allowing_clears_the_status_and_blocking_keeps_it_failing(live):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [
        {"alert_id": "a1", "decision": "allow", "reason": "known internal test fixture"},
        {"alert_id": "a2", "decision": "block", "reason": "it really does leak a phone number"}])
    relay.proceed("pandayv/micro-finance", 2)

    sha, state, description = live.statuses[-1]
    assert state == "failure" and "blocked by a reviewer" in description     # a2 is real: the PR stays blocked

    # Allowing the other one on its own clears the block.
    db2 = live
    db2.alerts["a2"].resolution, db2.alerts["a2"].status = "allowed", "resolved"
    relay.finding_detail("a1")
    relay._write_back([db2.alerts["a1"]])
    assert live.statuses[-1][1] == "success"


def test_the_status_goes_to_the_current_head_not_the_one_stored_on_the_alert(live):
    relay.finding_detail("a1")
    relay.request_risk_acceptance("a1", reason="known internal test fixture")
    assert live.alerts["a1"].head_sha == "abc123"
    assert all(sha == "currenthead" for sha, _, _ in live.statuses) and live.statuses


def test_a_block_by_voice_says_the_pr_stays_blocked_on_the_pr_comment(live):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [
        {"alert_id": "a1", "decision": "block", "reason": "it really does leak a phone number"}])
    relay.proceed("pandayv/micro-finance", 2)
    comment = next(c for c in live.comments if "Blocked by voice" in c)
    assert "stays blocked" in comment and "it really does leak a phone number" in comment


def test_an_allow_by_voice_says_the_risk_was_accepted_on_the_pr_comment(live):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [
        {"alert_id": "a1", "decision": "allow", "reason": "known internal test fixture"}])
    relay.proceed("pandayv/micro-finance", 2)
    comment = next(c for c in live.comments if "Allowed by voice" in c)
    assert "accepted the risk" in comment and "known internal test fixture" in comment


def test_a_blocked_finding_is_decided_so_it_is_not_asked_about_again_but_still_counts_as_blocking(live):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [
        {"alert_id": "a1", "decision": "block", "reason": "it really does leak a phone number"},
        {"alert_id": "a2", "decision": "block", "reason": "this one leaks an email as well"}])
    relay.proceed("pandayv/micro-finance", 2)

    assert relay.blocked_pull_requests()["pull_requests"] == []              # nothing left to review
    payload = relay.blocked_pull_requests()
    assert payload["summary"]["held"] == 2 and payload["held"][0]["pr_number"] == 2
    assert live.statuses[-1][1] == "failure"


# --- staging and batch resolution (docs/voice-resolution-workflow.md) ---


def _decision(alert_id, decision="allow", reason="it is only test data", **extra):
    return {"alert_id": alert_id, "decision": decision, "reason": reason, **extra}


def test_stage_without_a_recent_review_refuses(two_blocking, session_table):
    # Same rule as the single-finding allow, checked per finding in a batch.
    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", position=1)])
    assert result["performed"] == 0
    assert "screen" in result["spoken_response"].lower()


def test_stage_by_an_id_that_is_not_an_open_finding_refuses_the_whole_batch(two_blocking, session_table, monkeypatch):
    monkeypatch.setattr(relay, "resolve_alert", lambda *a, **k: pytest.fail("must not resolve anything"))
    relay.blocked_pull_requests()  # reviews both findings

    result = relay.stage_decisions("pandayv/micro-finance", 2, [
        _decision("a1"),
        _decision("gone-finding", "block", "needs a fix", position=5),
    ])

    assert result["performed"] == 0 and result["refusal"] == "not_found"
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_a_finding_decided_in_the_console_since_the_screen_was_drawn_refuses_honestly(live):
    relay.blocked_pull_requests()
    live.alerts["a2"].status, live.alerts["a2"].resolution = "resolved", "allowed"      # someone else decided it

    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1"), _decision("a2", position=2)])

    assert result["performed"] == 0 and result["refusal"] == "already_resolved"
    assert "finding 2" in result["spoken_response"].lower() and "console" in result["spoken_response"]
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_a_decision_names_the_finding_not_the_position(live):
    # The person looked at a screen where a2 was number 2. A higher-risk finding then arrived
    # and took number 1, so the live "second one" is a1. The decision carries a2's id, so it
    # lands on a2 whatever number it has now.
    relay.blocked_pull_requests()
    newcomer = _alert(alert_id="a0", score=9.9)
    newcomer.fragment_key = "frag-a0"
    live.alerts["a0"] = newcomer
    session_store.mark_shown("a0")      # seen now, so only the pointing differs

    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a2", position=2)])

    staged = pending_store.get_pending("pandayv/micro-finance", 2)
    assert result["staged"] == 1 and [d["alert_id"] for d in staged] == ["a2"]


def test_a_decision_without_an_id_is_refused_not_guessed_from_a_position(two_blocking, session_table):
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2,
                                   [{"position": 1, "decision": "allow", "reason": "it is only test data"}])
    assert result["performed"] == 0 and result["refusal"] == "missing_alert_id"


def test_the_same_finding_twice_in_one_batch_is_refused(two_blocking, session_table):
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1"), _decision("a1", "block")])
    assert result["refusal"] == "duplicate"


@pytest.mark.parametrize("bad", [True, False, "true", "false", 1, 0, None, "", "Allow", "BLOCK", " allow", "allow ", "approve",
                                 "reject", "approved", "allowed", "blocked", ["allow"], {"decision": "allow"}])
def test_a_decision_that_is_not_exactly_allow_or_block_is_refused_and_nothing_is_staged(two_blocking, session_table, bad):
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", decision=bad)])
    assert result["performed"] == 0 and result["refusal"] == "decision_unclear"
    assert "allow or block" in result["spoken_response"] and len(result["spoken_response"]) <= SPEECH_CHAR_BUDGET
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_a_missing_decision_is_refused(two_blocking, session_table):
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2, [{"alert_id": "a1", "reason": "it is only test data"}])
    assert result["refusal"] == "decision_unclear" and pending_store.get_pending("pandayv/micro-finance", 2) is None


@pytest.mark.parametrize("item", [
    {"alert_id": "a1", "approved": True, "reason": "it is only test data"},
    {"alert_id": "a1", "approved": False, "reason": "it is only test data"},
    {"alert_id": "a1", "approved": "false", "reason": "it is only test data"},
    {"alert_id": "a1", "approved": False, "decision": "allow", "reason": "it is only test data"},      # conflicting: never resolved by guessing
])
def test_the_old_approved_argument_is_refused_with_a_clear_sentence(two_blocking, session_table, monkeypatch, item):
    monkeypatch.setattr(relay, "resolve_alert", lambda *a, **k: pytest.fail("must not resolve"))
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2, [item])
    assert result["performed"] == 0 and result["refusal"] == "approved_argument_retired"
    spoken = result["spoken_response"]
    assert '"allow"' in spoken and '"block"' in spoken and len(spoken) <= SPEECH_CHAR_BUDGET
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_the_unclear_decision_refusal_leaves_an_audit_code_without_the_reason(two_blocking, session_table, caplog):
    import logging
    relay.blocked_pull_requests()
    with caplog.at_level(logging.INFO, logger="ship.relay.audit"):
        relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", decision="maybe", reason="secret words")])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "why=decision_unclear" in text and "approved_unclear" not in text and "secret words" not in text


def test_stage_missing_reason_refuses(two_blocking, session_table):
    relay.blocked_pull_requests()
    result = relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", reason="", position=1)])
    assert result["performed"] == 0
    assert "reason" in result["spoken_response"].lower()


def test_stage_valid_batch_captures_but_does_not_resolve(two_blocking, session_table, monkeypatch):
    monkeypatch.setattr(relay, "resolve_alert", lambda *a, **k: pytest.fail("staging must not resolve"))
    relay.blocked_pull_requests()

    result = relay.stage_decisions("pandayv/micro-finance", 2, [
        _decision("a1", "allow", "non-issue", position=1),
        _decision("a2", "block", "needs a fix", position=2),
    ])

    assert result["staged"] == 2 and result["performed"] == 0
    staged = pending_store.get_pending("pandayv/micro-finance", 2)
    assert {d["alert_id"]: d["decision"] for d in staged} == {"a1": "allow", "a2": "block"}
    assert all("approved" not in d for d in staged)


def test_proceed_with_nothing_staged_says_so(two_blocking, session_table):
    result = relay.proceed("pandayv/micro-finance", 2)
    assert result["performed"] == 0 and result["outcomes"] == []
    assert "nothing" in result["spoken_response"].lower()


def _stage_both(first="allow"):
    relay.blocked_pull_requests()
    return relay.stage_decisions("pandayv/micro-finance", 2, [
        _decision("a1", first, "non-issue"), _decision("a2", "block", "needs a fix")])


def test_proceed_commits_every_staged_decision_and_clears_pending(live):
    _stage_both()
    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 2 and type(result["performed"]) is int
    assert [o["outcome"] for o in result["outcomes"]] == ["applied", "applied"]
    assert result["spoken_response"] == "Done. Applied all 2."
    assert set(live.resolve_calls) == {"a1", "a2"}
    assert pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_proceed_rechecks_staleness_at_commit_time_not_just_at_staging(live, session_table):
    # The review window could lapse between "I've reviewed these" and "proceed".
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", "allow", "fine and dandy")])

    session_table.rows["a1"]["shown_at"] -= session_store.RECENT_WINDOW_SECONDS + 1
    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 0 and live.resolve_calls == []
    assert result["outcomes"] == [{"alert_id": "a1", "outcome": "skipped_not_seen"}]
    assert "shown on a screen" in result["spoken_response"] and result["spoken_response"] != "Done."


def test_proceed_says_what_it_did_when_some_are_gone_or_already_decided(live):
    _stage_both()
    live.alerts["a2"].status, live.alerts["a2"].resolution = "resolved", "allowed"      # decided in the console
    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 1
    assert {o["alert_id"]: o["outcome"] for o in result["outcomes"]} == {"a1": "applied", "a2": "already_resolved"}
    assert result["spoken_response"] == "Applied 1 of 2. 1 already decided in the console."


def test_proceed_reports_a_finding_that_no_longer_exists(live):
    _stage_both()
    del live.alerts["a2"]
    result = relay.proceed("pandayv/micro-finance", 2)
    assert {o["alert_id"]: o["outcome"] for o in result["outcomes"]}["a2"] == "not_found"
    assert result["spoken_response"] == "Applied 1 of 2. 1 no longer exists."


def test_a_race_lost_at_the_database_is_reported_per_item_and_does_not_abort_the_batch(live, monkeypatch):
    _stage_both()
    original = live.resolve

    def lose_the_first(alert_id, decision, reason, resolved_by):
        if alert_id == "a1":
            raise AlertNotFoundOrAlreadyResolved(alert_id)
        return original(alert_id, decision, reason, resolved_by)

    monkeypatch.setattr(relay, "resolve_alert", lose_the_first)
    result = relay.proceed("pandayv/micro-finance", 2)
    assert {o["alert_id"]: o["outcome"] for o in result["outcomes"]} == {"a1": "already_resolved", "a2": "applied"}


def test_a_temporary_failure_keeps_the_decision_staged_so_proceed_can_be_repeated(live, monkeypatch):
    _stage_both()
    original = live.resolve
    broken = {"on": True}

    def flaky(alert_id, decision, reason, resolved_by):
        if broken["on"]:
            raise RuntimeError("throttled")
        return original(alert_id, decision, reason, resolved_by)

    monkeypatch.setattr(relay, "resolve_alert", flaky)
    first = relay.proceed("pandayv/micro-finance", 2)

    # Nothing was applied because of a transient problem: nothing is cleared.
    assert first["performed"] == 0 and first["pending_remaining"] == 2
    assert {o["outcome"] for o in first["outcomes"]} == {"error"}
    assert first["spoken_response"] == "I couldn't save that just now. Say proceed to try again."
    assert len(pending_store.get_pending("pandayv/micro-finance", 2)) == 2

    broken["on"] = False
    second = relay.proceed("pandayv/micro-finance", 2)
    assert second["performed"] == 2 and pending_store.get_pending("pandayv/micro-finance", 2) is None


def test_part_applied_part_failed_keeps_only_the_failed_one_staged(live, monkeypatch):
    _stage_both()
    original = live.resolve
    monkeypatch.setattr(relay, "resolve_alert", lambda alert_id, decision, reason, resolved_by: (
        (_ for _ in ()).throw(RuntimeError("throttled")) if alert_id == "a2" else original(alert_id, decision, reason, resolved_by)))
    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 1 and result["pending_remaining"] == 1
    assert [d["alert_id"] for d in pending_store.get_pending("pandayv/micro-finance", 2)] == ["a2"]
    assert result["spoken_response"] == "Applied 1 of 2. 1 could not be saved, say proceed to retry."


def test_every_proceed_sentence_fits_the_speech_cap_even_with_a_big_mixed_batch():
    outcomes = ([{"outcome": "applied"}] * 10 + [{"outcome": "skipped_not_seen"}] * 10 + [{"outcome": "already_resolved"}] * 10
                + [{"outcome": "not_found"}] * 10 + [{"outcome": "unreadable"}] * 10 + [{"outcome": "error"}] * 10)
    assert len(relay._proceed_sentence(outcomes)) <= SPEECH_CHAR_BUDGET


def test_performed_is_an_integer_in_every_tool_that_reports_it(live):
    results = [relay.request_risk_acceptance("a1"), relay.stage_decisions("pandayv/micro-finance", 2, []),
               relay.proceed("pandayv/micro-finance", 2), relay.cancel_pending("pandayv/micro-finance", 2)]
    assert all(type(r["performed"]) is int for r in results)


def test_proceed_is_the_tool_name_exposed_alongside_stage(two_blocking):
    names = {t.name for t in asyncio.run(mcp_tools())}
    assert {"stage_decisions", "proceed"} <= names


def test_findings_routing_reports_the_number_each_finding_had_when_it_was_shown(live):
    relay.blocked_pull_requests()
    newcomer = _alert(alert_id="a0", score=9.9)
    live.alerts["a0"] = newcomer       # arrives after the screen: live number 1, never shown

    findings = {f["alert_id"]: f for f in relay.findings_routing()["pull_requests"][0]["findings"]}
    assert findings["a2"]["position"] == 3 and findings["a2"]["shown_position"] == 2     # the number the person saw
    assert findings["a0"]["seen"] is False and findings["a0"]["shown_position"] is None


def test_a_closed_summary_read_is_not_all_clear(monkeypatch):
    def unreadable():
        raise SummaryUnavailable("down")
    monkeypatch.setattr("src.relay.readmodel.read_summary", unreadable)
    spoken = relay.release_status()
    assert "can't check" in spoken and "review console" in spoken
    assert "move ahead" not in spoken and "no blocking" not in spoken.lower()


def test_blocked_findings_that_still_hold_the_merge_are_never_called_all_clear(monkeypatch):
    summary = ReleaseSummary(held=2, pull_requests=[PullRequestCounts("a/b", 3, 0, 0, "2026-09-24", held=2)])
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: summary)
    spoken = relay.release_status()
    assert "move ahead" not in spoken
    assert spoken == "Nothing needs a decision, but 2 findings that were blocked still hold the merge until the code is fixed."


def test_the_status_offer_names_only_screens_that_exist(one_blocking, monkeypatch):
    assert "screen" not in relay.release_status()                     # nothing connected: nothing to offer
    monkeypatch.setattr(relay, "list_all_connections", lambda: [_Conn("laptop")])
    assert relay.release_status().endswith("Want it on the laptop?")
    monkeypatch.setattr(relay, "list_all_connections", lambda: [_Conn("laptop"), _Conn("tablet")])
    assert relay.release_status().endswith("Want it on the laptop or the tablet?")
    monkeypatch.setattr(relay, "list_all_connections", lambda: [_Conn(n) for n in ("a", "b", "c")])
    assert relay.release_status().endswith("Want it on a screen?")


def test_a_failure_listing_screens_does_not_stop_the_status_answer(one_blocking, monkeypatch):
    def broken():
        raise RuntimeError("table down")
    monkeypatch.setattr(relay, "list_all_connections", broken)
    assert "1 blocker found" in relay.release_status()


def test_a_screen_name_that_is_too_long_to_speak_cannot_break_display_on(one_blocking, monkeypatch):
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": [], "not_connected": True})
    result = relay.display_on("x" * 500)
    assert len(result["spoken_response"]) <= SPEECH_CHAR_BUDGET


def test_every_tool_decision_leaves_an_audit_line_without_the_reason_text(live, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="ship.relay.audit"):
        relay.finding_detail("a1")
        relay.request_risk_acceptance("a1", reason="secret reason words")
        relay.request_risk_acceptance("a2", reason="")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "tool=request_risk_acceptance outcome=applied" in text and "outcome=refused" in text and "why=not_seen" in text
    assert "secret reason words" not in text


# --- routing ---


def test_display_on_here_never_touches_the_push_path(one_blocking, monkeypatch):
    # "here" means the asking device renders its own copy — no push, no
    # network call. Proven by making push_to_device explode if it's ever
    # called, not just by asserting the surface name.
    def _must_not_be_called(*a, **k):
        raise AssertionError("display_on('here') must not call push_to_device")
    monkeypatch.setattr(relay, "push_to_device", _must_not_be_called)

    result = relay.display_on("")
    assert result["surface"] == "here"
    assert result["delivered"] is True


def test_display_on_pushes_to_a_connected_device(one_blocking, monkeypatch):
    calls = []
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: calls.append((name, payload)) or {
        "delivered_to": ["conn-1"], "not_connected": False,
    })

    result = relay.display_on("iPad")
    assert calls[0][0] == "ipad"  # normalised to lowercase before pushing
    assert result["delivered"] is True
    assert "ipad" in result["spoken_response"].lower()
    assert "putting it on" in result["spoken_response"].lower()


def test_display_on_reports_honestly_when_nothing_is_connected(one_blocking, monkeypatch):
    # The device not being connected is a routine, expected case (an
    # ambient display that happens to be off) — not an error, but the
    # spoken response must say so honestly rather than claim delivery
    # that didn't happen.
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {
        "delivered_to": [], "not_connected": True,
    })

    result = relay.display_on("fridge")
    assert result["delivered"] is False
    assert "don't see" in result["spoken_response"].lower()


def test_display_on_pushes_the_real_findings_payload(one_blocking, monkeypatch):
    # What actually gets pushed must be the same shape blocked_pull_requests
    # itself returns — a display page renders it with the identical code
    # path as the simulator's own local "show here" case.
    captured = {}
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: captured.update(payload=payload) or {
        "delivered_to": ["c1"], "not_connected": False,
    })

    relay.display_on("tv")
    assert captured["payload"]["display_only"] is True
    assert captured["payload"]["pull_requests"][0]["repo"] == "pandayv/micro-finance"


# --- the summary voice is allowed to give ---


def test_spoken_blocking_single_pr():
    spoken = _status([_alert(pr=1, severity="blocking")]).to_spoken().to_text().lower()
    assert "1 blocker found" in spoken
    assert "ready for your decision" in spoken
    assert "across" not in spoken  # no PR count for a single PR


def test_spoken_blocking_multiple_prs():
    spoken = _status([
        _alert(repo="a/one", pr=1, severity="blocking", alert_id="x1"),
        _alert(repo="b/two", pr=2, severity="review", score=6.0, alert_id="x2"),
    ]).to_spoken().to_text().lower()
    assert "1 blocker found across 2 prs" in spoken
    assert "ready for your decision" in spoken


def test_spoken_mixed_severity_states_only_the_blocking_count():
    # A blocking finding and a separate, non-blocking one on the SAME
    # PR — the REVIEW-band one is still a real, dismissable Gate alert
    # (see ALERTING_ACTIONS in src/agents/triage.py), just not spoken
    # here, to avoid implying it's the blocking ones that aren't reviewable.
    spoken = _status([
        _alert(pr=1, severity="blocking", alert_id="x1"),
        _alert(pr=1, severity="review", score=6.0, alert_id="x2"),
    ]).to_spoken().to_text().lower()
    assert "1 blocker found" in spoken
    assert "2" not in spoken  # the review-band count never reaches voice


def test_spoken_review_only_single_pr():
    spoken = _status([_alert(pr=1, severity="review", score=6.0)]).to_spoken().to_text().lower()
    assert "1 issue found" in spoken
    assert "none blocking" in spoken
    assert "ready for your review" in spoken


def test_spoken_review_only_multiple_prs():
    spoken = _status([
        _alert(repo="a/one", pr=1, severity="review", score=6.0, alert_id="x1"),
        _alert(repo="b/two", pr=2, severity="review", score=6.0, alert_id="x2"),
    ]).to_spoken().to_text().lower()
    assert "2 issues found across 2 prs" in spoken
    assert "none blocking" in spoken


def test_missing_finding_is_reported_not_faked(one_blocking):
    assert relay.finding_detail("does-not-exist")["found"] is False


# --- Lambda execution model ---


def test_create_app_returns_a_new_instance_every_call():
    # The MCP SDK's StreamableHTTP session manager can only run its lifespan once per instance. A
    # module-level app object reused across Lambda invocations crashes on the SECOND request with
    # "SessionManager .run() can only be called once per instance". create_app() must hand back a
    # fresh, never-run instance every time it is called, not the same object memoized.
    from src.relay.server import create_app

    apps = [create_app() for _ in range(3)]
    assert len(apps) == len({id(a) for a in apps}), "create_app() returned the same instance twice"


def test_relay_lambda_handler_serves_three_independent_invocations(monkeypatch):
    # The actual failure mode, exercised through the real Lambda entrypoint
    # AWS will call — not just create_app() in isolation. This is what a
    # single happy-path test (one call, looks fine) would have missed: the
    # bug only appeared on the SECOND request.
    import json

    # ALLOWED_HOSTS is computed once at module import time from the env
    # var (see src/relay/server.py) — by the time this test runs, that
    # module is already imported, so setting the env var here would do
    # nothing. Patch the module attribute create_app() actually reads.
    monkeypatch.setattr("src.relay.server.ALLOWED_HOSTS", ["x.lambda-url.us-west-2.on.aws"])
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: __import__(
        "src.storage.status_store", fromlist=["ReleaseSummary"]).ReleaseSummary())

    import relay_lambda_handler

    def event(request_id):
        return {
            "version": "2.0", "routeKey": "POST /mcp", "rawPath": "/mcp", "rawQueryString": "",
            "headers": {"content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                        "host": "x.lambda-url.us-west-2.on.aws"},
            "requestContext": {"http": {"method": "POST", "path": "/mcp", "sourceIp": "1.2.3.4"},
                              "stage": "$default", "requestId": str(request_id),
                              "domainName": "x.lambda-url.us-west-2.on.aws"},
            "body": json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": {}}),
            "isBase64Encoded": False,
        }

    statuses = [relay_lambda_handler.handler(event(i), None)["statusCode"] for i in range(3)]
    assert statuses == [200, 200, 200]


# --- CORS: a browser enforces this independently of transport_security ---


def _cors_event(method, origin, extra_headers=None):
    import json
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream",
               "host": "x.lambda-url.us-west-2.on.aws"}
    if origin:
        headers["origin"] = origin
    if extra_headers:
        headers.update(extra_headers)
    return {
        "version": "2.0", "routeKey": f"{method} /mcp", "rawPath": "/mcp", "rawQueryString": "",
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": "/mcp", "sourceIp": "1.2.3.4"},
                          "stage": "$default", "requestId": "t", "domainName": "x.lambda-url.us-west-2.on.aws"},
        "body": json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) if method == "POST" else None,
        "isBase64Encoded": False,
    }


def test_allowed_origin_gets_real_cors_headers(monkeypatch):
    # transport_security alone is a deny-gate: it rejects a disallowed
    # origin, but a real local test proved it does NOT add
    # Access-Control-Allow-Origin even for an allowed one. A browser
    # enforces CORS independently of what the server permits — without
    # this header, a genuine cross-origin fetch() would succeed on the
    # wire and then be silently blocked from JS reading the response,
    # visible only in a browser console, never to curl or this test suite
    # if it only checked status codes.
    import json

    from mangum import Mangum

    monkeypatch.setattr("src.relay.server.ALLOWED_HOSTS", ["x.lambda-url.us-west-2.on.aws"])
    monkeypatch.setattr("src.relay.server.ALLOWED_ORIGINS", ["https://pandayv.github.io"])
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: __import__(
        "src.storage.status_store", fromlist=["ReleaseSummary"]).ReleaseSummary())

    from src.relay.server import create_app

    r = Mangum(create_app())(_cors_event("POST", "https://pandayv.github.io"), None)
    assert r["statusCode"] == 200
    assert r["headers"].get("access-control-allow-origin") == "https://pandayv.github.io"


def test_disallowed_origin_is_rejected_with_no_cors_headers_leaked(monkeypatch):
    from mangum import Mangum

    monkeypatch.setattr("src.relay.server.ALLOWED_HOSTS", ["x.lambda-url.us-west-2.on.aws"])
    monkeypatch.setattr("src.relay.server.ALLOWED_ORIGINS", ["https://pandayv.github.io"])

    from src.relay.server import create_app

    r = Mangum(create_app())(_cors_event("POST", "https://evil.example"), None)
    assert r["statusCode"] == 403
    assert "access-control-allow-origin" not in r["headers"]


def test_preflight_options_request_succeeds(monkeypatch):
    from mangum import Mangum

    monkeypatch.setattr("src.relay.server.ALLOWED_HOSTS", ["x.lambda-url.us-west-2.on.aws"])
    monkeypatch.setattr("src.relay.server.ALLOWED_ORIGINS", ["https://pandayv.github.io"])

    from src.relay.server import create_app

    event = _cors_event("OPTIONS", "https://pandayv.github.io", {
        "access-control-request-method": "POST", "access-control-request-headers": "content-type",
    })
    r = Mangum(create_app())(event, None)
    assert r["statusCode"] == 200
    assert r["headers"].get("access-control-allow-origin") == "https://pandayv.github.io"
    assert "POST" in r["headers"].get("access-control-allow-methods", "")


# --- the voice gate and what counts as "shown" ---


def test_a_screen_that_is_not_connected_does_not_count_as_having_shown_anything(one_blocking, session_table, monkeypatch):
    # The failure this guards: "show it on the fridge" (not connected) used to
    # record the findings as seen, so "allow it because..." then went through
    # although no screen had displayed a thing.
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": [], "not_connected": True})
    monkeypatch.setattr(relay, "resolve_alert", lambda *a, **k: pytest.fail("must not resolve"))

    shown = relay.display_on("fridge")
    result = relay.request_risk_acceptance("a1", reason="looks fine to me")

    assert shown["delivered"] is False
    assert shown["routing"] == []
    assert result["performed"] == 0


def test_a_delivered_push_returns_routing_with_positions_and_ids_only(one_blocking, session_table, monkeypatch):
    monkeypatch.setattr(relay, "push_to_device", lambda name, payload: {"delivered_to": ["c1"], "not_connected": False})

    result = relay.display_on("tv")

    finding = result["routing"][0]["findings"][0]
    assert result["routing"][0]["repo"] == "pandayv/micro-finance"
    assert finding["position"] == 1 and finding["alert_id"] == "a1"
    flat = str(result["routing"])
    assert "citation" not in flat and "file" not in flat and "what_is_wrong" not in flat


# --- which screens are set up ---


class _Conn:
    def __init__(self, name):
        self.device_name = name


def test_connected_screens_lists_each_name_once_sorted(monkeypatch):
    monkeypatch.setattr(relay, "list_all_connections", lambda: [_Conn("tv"), _Conn("laptop"), _Conn("tv")])
    assert relay.connected_screens()["screens"] == ["laptop", "tv"]


def test_connected_screens_is_empty_when_no_display_page_is_open(monkeypatch):
    monkeypatch.setattr(relay, "list_all_connections", lambda: [])
    assert relay.connected_screens()["screens"] == []


def test_connected_screens_is_one_of_relays_tools():
    assert "connected_screens" in {t.__name__ for t in relay.TOOLS}


def test_cancel_pending_drops_the_staged_decisions_without_resolving_anything(two_blocking, session_table, monkeypatch):
    from src.storage import pending_store
    cleared = []
    monkeypatch.setattr(pending_store, "clear_pending", lambda repo, pr: cleared.append((repo, pr)))
    resolved = []
    monkeypatch.setattr(relay, "_resolve_one", lambda *a, **k: resolved.append(a))
    result = relay.cancel_pending("pandayv/micro-finance", 2)
    assert cleared == [("pandayv/micro-finance", 2)] and resolved == []
    assert result["performed"] == 0 and "won't apply" in result["spoken_response"]


def test_a_staged_confirmation_that_has_gone_stale_is_not_acted_on(live, pending_table):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", "allow", "fine and dandy")])
    pending_table.rows["pandayv/micro-finance#2"]["staged_at"] -= pending_store.STALE_AFTER_SECONDS + 1

    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 0 and live.resolve_calls == []
    assert "Nothing pending" in result["spoken_response"]


def test_a_pending_read_failure_reads_as_nothing_pending_and_resolves_nothing(live, monkeypatch):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1", "allow", "fine and dandy")])
    monkeypatch.setattr(pending_store, "_table", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert relay.proceed("pandayv/micro-finance", 2)["performed"] == 0 and live.resolve_calls == []


def test_a_failed_session_read_fails_closed_for_every_decision_path(live, monkeypatch):
    relay.blocked_pull_requests()
    monkeypatch.setattr(session_store, "_table", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert relay.request_risk_acceptance("a1", reason="known internal test fixture")["performed"] == 0
    assert relay.stage_decisions("pandayv/micro-finance", 2, [_decision("a1")])["performed"] == 0
    assert live.resolve_calls == []


# --- the decision is a word, and an unreadable staged row is never applied ---


def _stage_old_row(pending_table, **overrides):
    """A row in ship-pending-dispositions as code that stored the decision another way left it."""
    import time
    row = {"alert_id": "a1", "position": 1, "approved": True, "reason": "it is only test data"}
    row.update(overrides)
    pending_table.rows["pandayv/micro-finance#2"] = {
        "pr_key": "pandayv/micro-finance#2", "staged_at": int(time.time()), "expires_at": 0, "decisions": [row]}


def test_proceed_never_applies_a_staged_row_that_has_no_decision(live, pending_table):
    relay.blocked_pull_requests()
    _stage_old_row(pending_table)

    result = relay.proceed("pandayv/micro-finance", 2)

    assert result["performed"] == 0 and live.resolve_calls == [] and live.alerts["a1"].status == "frozen"
    assert result["outcomes"] == [{"alert_id": "a1", "outcome": "unreadable"}]
    assert result["spoken_response"] == "I couldn't read what was staged, so I applied nothing. Please say your decisions again."
    assert len(result["spoken_response"]) <= SPEECH_CHAR_BUDGET
    assert result["pending_remaining"] == 0 and pending_store.get_pending("pandayv/micro-finance", 2) is None


@pytest.mark.parametrize("bad", ["Allow", "approve", True, None, "", "allowed"])
def test_proceed_never_applies_a_staged_row_whose_decision_is_not_exactly_allow_or_block(live, pending_table, bad):
    relay.blocked_pull_requests()
    _stage_old_row(pending_table, decision=bad)
    result = relay.proceed("pandayv/micro-finance", 2)
    assert result["performed"] == 0 and live.resolve_calls == []
    assert [o["outcome"] for o in result["outcomes"]] == ["unreadable"]


def test_proceed_with_a_row_that_is_not_even_a_record_does_not_crash(live, pending_table):
    import time
    pending_table.rows["pandayv/micro-finance#2"] = {
        "pr_key": "pandayv/micro-finance#2", "staged_at": int(time.time()), "expires_at": 0, "decisions": ["a1", {"reason": "x"}]}
    result = relay.proceed("pandayv/micro-finance", 2)
    assert result["performed"] == 0 and live.resolve_calls == []
    assert [o["outcome"] for o in result["outcomes"]] == ["unreadable", "unreadable"]


def test_a_readable_row_is_still_applied_next_to_an_unreadable_one_and_the_sentence_says_so(live, pending_table):
    import time
    relay.blocked_pull_requests()
    pending_table.rows["pandayv/micro-finance#2"] = {
        "pr_key": "pandayv/micro-finance#2", "staged_at": int(time.time()), "expires_at": 0,
        "decisions": [{"alert_id": "a1", "position": 1, "decision": "block", "reason": "it leaks a phone number"},
                      {"alert_id": "a2", "position": 2, "approved": True, "reason": "it is only test data"}]}

    result = relay.proceed("pandayv/micro-finance", 2)

    assert {o["alert_id"]: o["outcome"] for o in result["outcomes"]} == {"a1": "applied", "a2": "unreadable"}
    assert live.alerts["a1"].resolution == "blocked" and live.alerts["a2"].status == "frozen"
    assert result["spoken_response"] == "Applied 1 of 2. 1 couldn't be read, say it again."


def test_a_staged_row_without_a_readable_decision_is_not_shown_as_pending_on_a_screen(live, pending_table):
    _stage_old_row(pending_table)
    findings = {f["alert_id"]: f for f in relay.blocked_pull_requests()["pull_requests"][0]["findings"]}
    assert findings["a1"]["pending"] is None


def test_a_staged_row_that_is_not_a_record_cannot_break_the_screen_payload(live, pending_table):
    import time
    pending_table.rows["pandayv/micro-finance#2"] = {
        "pr_key": "pandayv/micro-finance#2", "staged_at": int(time.time()), "expires_at": 0, "decisions": ["a1", {"reason": "x"}]}
    findings = relay.blocked_pull_requests()["pull_requests"][0]["findings"]
    assert [f["pending"] for f in findings] == [None, None]


def test_a_staged_decision_reaches_the_screen_as_decision_and_reason(live):
    relay.blocked_pull_requests()
    relay.stage_decisions("pandayv/micro-finance", 2, [
        _decision("a1", "allow", "known internal test fixture"), _decision("a2", "block", "it leaks a phone number")])
    findings = {f["alert_id"]: f for f in relay.blocked_pull_requests()["pull_requests"][0]["findings"]}
    assert findings["a1"]["pending"] == {"decision": "allow", "reason": "known internal test fixture"}
    assert findings["a2"]["pending"] == {"decision": "block", "reason": "it leaks a phone number"}
    assert "approved" not in str(findings)


def test_the_stored_resolution_is_allowed_or_blocked(live):
    _stage_both()
    relay.proceed("pandayv/micro-finance", 2)
    assert live.alerts["a1"].resolution == "allowed" and live.alerts["a2"].resolution == "blocked"


def test_the_one_step_tool_allows_and_says_so(live):
    relay.finding_detail("a1")
    result = relay.request_risk_acceptance("a1", reason="known internal test fixture")
    assert result["spoken_response"] == "Done. Allowed, on the record."
    assert any("Allowed by voice" in c for c in live.comments)


# --- the words Relay speaks and shows ---


@pytest.mark.parametrize("held,expected", [
    (1, "Nothing needs a decision, but 1 finding that was blocked still holds the merge until the code is fixed."),
    (2, "Nothing needs a decision, but 2 findings that were blocked still hold the merge until the code is fixed."),
    (12, "Nothing needs a decision, but 12 findings that were blocked still hold the merge until the code is fixed."),
])
def test_the_held_count_sentence_uses_the_new_vocabulary_and_fits_the_cap(monkeypatch, held, expected):
    summary = ReleaseSummary(held=held, pull_requests=[PullRequestCounts("a/b", 3, 0, 0, "2026-09-24", held=held)])
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: summary)
    spoken = relay.release_status()
    assert spoken == expected and len(spoken) <= SPEECH_CHAR_BUDGET
    assert "reject" not in spoken.lower()


@pytest.mark.parametrize("held,expected_tail", [(1, "1 blocked finding still holds the merge."),
                                                (3, "3 blocked findings still hold the merge.")])
def test_the_held_count_next_to_review_only_findings_reads_naturally(held, expected_tail):
    grouped = _group([_alert(severity="review", score=6.0)])
    status = ReleaseStatus([replace(grouped[0], held=held)])
    spoken = status.to_spoken().to_text()
    assert spoken == f"1 issue found. {expected_tail}" and len(spoken) <= SPEECH_CHAR_BUDGET


def test_every_new_refusal_sentence_fits_the_speech_cap():
    from src.relay.modality import Spoken as _Spoken
    for sentence in ("I couldn't tell whether to allow or block that. Which was it?",
                     'Decisions no longer take "approved". Send decision as "allow" or "block".',
                     "I couldn't read what was staged, so I applied nothing. Please say your decisions again.",
                     "Done. Allowed, on the record."):
        assert len(_Spoken(sentence).to_text()) <= SPEECH_CHAR_BUDGET


def test_tool_descriptions_stay_inside_the_900_characters_the_model_is_given():
    # The skill hands the model description[:900]. Python before 3.13 keeps the docstring's indentation,
    # which the Lambda runtime (3.12) does, so measure with it put back.
    import inspect
    for tool in relay.TOOLS:
        text = "\n".join(("    " + line if line else line) for line in inspect.getdoc(tool).splitlines())
        assert len(text) < 900, tool.__name__


def test_the_tool_descriptions_teach_allow_and_block_and_not_approve_or_reject():
    import inspect
    text = " ".join(inspect.getdoc(t) for t in (relay.stage_decisions, relay.request_risk_acceptance, relay.proceed))
    text += relay.INSTRUCTIONS
    assert '"allow"' in text and '"block"' in text
    for old in ("approved (", "approve ", "reject ", "rejects", "approves"):
        assert old not in text.lower(), old


@pytest.mark.parametrize("held,tail", [(1, " 1 blocked finding still holds the merge."), (3, " 3 blocked findings still hold the merge.")])
def test_a_finding_the_reviewer_blocked_is_still_counted_when_others_need_a_decision(held, tail):
    # Seen live: after blocking one of three, the status said "1 blocker" and the blocked one
    # disappeared from what Alexa said, although the pull request was still blocked by it.
    grouped = _group([_alert(severity="blocking", score=9.0)])
    status = ReleaseStatus([replace(grouped[0], held=held)])
    spoken = status.to_spoken().to_text()
    assert spoken == "1 blocker found. Ready for your decision." + tail and len(spoken) <= SPEECH_CHAR_BUDGET


def test_the_sentence_is_unchanged_when_nothing_is_blocked():
    grouped = _group([_alert(severity="blocking", score=9.0)])
    assert ReleaseStatus(grouped).to_spoken().to_text() == "1 blocker found. Ready for your decision."


@pytest.mark.parametrize("given", [
    "https://console.example.test", "https://console.example.test/", "https://console.example.test/dashboard",
    "https://console.example.test/dashboard/", "  https://console.example.test/dashboard  ",
])
def test_the_console_address_works_whether_it_is_given_as_the_site_root_or_the_dashboard_url(given):
    # The webhook is given ".../dashboard"; one value should serve Relay too, without doubling the path.
    assert relay._console_root(given) == "https://console.example.test"


def test_with_no_console_address_the_reply_names_the_console_instead_of_linking_to_it(monkeypatch):
    monkeypatch.setattr(relay, "CONSOLE_URL", "")
    assert relay._console("/dashboard") == "the SHIP review console"
    monkeypatch.setattr(relay, "CONSOLE_URL", "https://console.example.test")
    assert relay._console("/dashboard") == "https://console.example.test/dashboard"
    assert relay._console("/dashboard#abc") == "https://console.example.test/dashboard#abc"
