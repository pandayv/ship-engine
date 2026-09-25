"""
Tests for Herald, SHIP's MCP server.

Two properties matter more than the rest and are asserted directly rather
than left to code review:

  1. Voice never narrates. A spoken response is one short sentence with no
     list, no citation and no code. This is enforced by the server, so a
     future prompt or a well-meaning change cannot quietly turn Herald
     into a narrator.
  2. No compliance risk can be accepted by voice. SHIP flags exactly this
     pattern in other people's code (TLGP-002 — an automated decision with
     no human checkpoint); doing it in its own interface would be the same
     violation. The tool surface must make it impossible, not merely
     unimplemented.
"""

import asyncio

import pytest

import src.herald.server as herald
from src.herald.modality import SPEECH_CHAR_BUDGET, ModalityViolation, Spoken
from src.herald.readmodel import ReleaseStatus, _group
from src.storage.alert_store import Alert


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


@pytest.fixture
def one_blocking(monkeypatch):
    alerts = [_alert()]
    monkeypatch.setattr("src.herald.readmodel.list_active_alerts", lambda: alerts)
    monkeypatch.setattr(herald, "get_alert", lambda aid: alerts[0] if aid == "a1" else None)
    return alerts


# --- property 1: voice never narrates ---


def test_spoken_status_fits_one_sentence(one_blocking):
    spoken = herald.release_status()
    assert len(spoken) <= SPEECH_CHAR_BUDGET + 40  # + the screen hint
    assert "\n" not in spoken


def test_spoken_status_never_leaks_screen_facts(one_blocking):
    # Citations, file paths, taxonomy ids and code are screen facts. If any
    # of them reach the spoken path, voice has started narrating.
    spoken = herald.release_status().lower()
    # Naming the pull request IS appropriate for voice — it is the
    # destination, not detail. Citations, files, taxonomy ids and code are
    # the screen facts that must never be spoken.
    for leak in ["gdpr", "art.", "piie", ".py", "hash identifiers", "risk score"]:
        assert leak not in spoken, f"spoken response leaked a screen fact: {leak!r}"


def test_a_long_list_cannot_be_spoken():
    # The budget is the enforcement: twelve findings cannot be compressed
    # into one sentence, so the attempt must fail loudly rather than
    # truncate into a half-read list.
    with pytest.raises(ModalityViolation):
        Spoken(", ".join(f"finding {i} in file_{i}.py" for i in range(12)))


def test_clean_state_is_reassuring_and_short(monkeypatch):
    monkeypatch.setattr("src.herald.readmodel.list_active_alerts", lambda: [])
    spoken = herald.release_status()
    assert "nothing is blocked" in spoken.lower()
    assert len(spoken) <= SPEECH_CHAR_BUDGET


def test_screen_tools_are_marked_display_only(one_blocking):
    assert herald.blocked_pull_requests()["display_only"] is True
    assert herald.finding_detail("a1")["display_only"] is True


def test_screen_tools_carry_the_detail_voice_withholds(one_blocking):
    detail = herald.finding_detail("a1")
    assert detail["citation"] == "GDPR Art. 32(1)(a)"
    assert detail["blocks_merge"] is True
    assert "hash identifiers" in detail["suggested_remediation"]


# --- property 2: voice cannot accept risk ---


def test_no_tool_exists_that_accepts_a_risk():
    names = {t.name for t in asyncio.run(mcp_tools())}
    forbidden = {"accept_risk", "approve", "approve_finding", "resolve_alert",
                 "override", "dismiss_finding", "unblock"}
    assert not (names & forbidden), f"a risk-acceptance tool is exposed: {names & forbidden}"


async def mcp_tools():
    return await herald.mcp.list_tools()


def test_requesting_acceptance_never_performs_it(one_blocking):
    result = herald.request_risk_acceptance("a1")
    assert result["performed"] is False
    assert result["reason"] == "requires_attested_human_decision"


def test_the_refusal_is_short_enough_to_speak(one_blocking):
    spoken = herald.request_risk_acceptance("a1")["spoken_response"]
    assert len(spoken) <= SPEECH_CHAR_BUDGET
    assert "written reason" in spoken.lower()


def test_refusal_routes_to_a_surface_where_the_decision_can_be_made(one_blocking, monkeypatch):
    monkeypatch.setattr(herald, "CONSOLE_URL", "https://ship.example")
    assert herald.request_risk_acceptance("a1")["console_url"].startswith("https://ship.example/dashboard")


# --- routing ---


def test_display_on_routes_to_the_chosen_surface(one_blocking):
    result = herald.display_on("TV")
    assert result["surface"] == "tv"
    assert "tv" in result["spoken_response"].lower()


def test_display_on_defaults_to_here_when_unspecified(one_blocking):
    assert herald.display_on("")["surface"] == "here"


# --- the summary voice is allowed to give ---


def test_blocking_findings_are_prioritised_over_volume():
    urgent = _status([
        _alert(repo="a/one", pr=1, severity="review", score=6.0, created="2026-09-24T01:00:00+00:00", alert_id="x1"),
        _alert(repo="a/one", pr=1, severity="review", score=6.0, created="2026-09-24T01:01:00+00:00", alert_id="x2"),
        _alert(repo="b/two", pr=2, severity="blocking", score=9.0, created="2026-09-24T02:00:00+00:00", alert_id="y1"),
    ]).most_urgent
    # One stopped merge outranks two advisory flags.
    assert urgent.label == "b/two#2"


def test_missing_finding_is_reported_not_faked(one_blocking):
    assert herald.finding_detail("does-not-exist")["found"] is False
