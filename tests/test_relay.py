"""
Tests for Relay, SHIP's MCP server.

Two properties matter more than the rest and are asserted directly rather
than left to code review:

  1. Voice never narrates. A spoken response is one short sentence with no
     list, no citation and no code. This is enforced by the server, so a
     future prompt or a well-meaning change cannot quietly turn Relay
     into a narrator.
  2. No compliance risk can be accepted by voice. SHIP flags exactly this
     pattern in other people's code (TLGP-002 — an automated decision with
     no human checkpoint); doing it in its own interface would be the same
     violation. The tool surface must make it impossible, not merely
     unimplemented.
"""

import asyncio

import pytest

import src.relay.server as relay
from src.relay.modality import SPEECH_CHAR_BUDGET, ModalityViolation, Spoken
from src.relay.readmodel import ReleaseStatus, _group
from src.storage.alert_store import Alert
from src.storage.status_store import PullRequestCounts, ReleaseSummary


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
    (blocked_pull_requests/finding_detail, backed by list_active_alerts())
    see consistent data — and neither ever reaches real AWS."""
    alerts = [_alert()]
    monkeypatch.setattr("src.relay.readmodel.list_active_alerts", lambda: alerts)
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: _summary_from(alerts))
    monkeypatch.setattr(relay, "get_alert", lambda aid: alerts[0] if aid == "a1" else None)
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
    # release_status() is the voice path and reads ONLY the pre-computed
    # summary (src/storage/status_store.py) — never list_active_alerts, and
    # never real AWS. Mocking read_summary() is what actually isolates
    # this test; mocking list_active_alerts() alone would silently fall
    # through to a real (and possibly non-empty) DynamoDB table.
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: ReleaseSummary())
    spoken = relay.release_status()
    assert "nothing is blocked" in spoken.lower()
    assert len(spoken) <= SPEECH_CHAR_BUDGET


def test_release_status_never_reads_the_alerts_table_directly(monkeypatch):
    # Regression test for the exact bug this fixture design just caught:
    # release_status() must depend ONLY on the pre-computed summary. If it
    # ever falls back to list_active_alerts() or a live scan, this proves
    # it by making that path explode.
    def _must_not_be_called():
        raise AssertionError("release_status() must not read list_active_alerts directly")

    monkeypatch.setattr("src.relay.readmodel.list_active_alerts", lambda: _must_not_be_called())
    monkeypatch.setattr("src.relay.readmodel.read_summary", lambda: ReleaseSummary())
    assert "nothing is blocked" in relay.release_status().lower()


def test_screen_tools_are_marked_display_only(one_blocking):
    assert relay.blocked_pull_requests()["display_only"] is True
    assert relay.finding_detail("a1")["display_only"] is True


def test_screen_tools_carry_the_detail_voice_withholds(one_blocking):
    detail = relay.finding_detail("a1")
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
    return await relay.mcp.list_tools()


def test_requesting_acceptance_never_performs_it(one_blocking):
    result = relay.request_risk_acceptance("a1")
    assert result["performed"] is False
    assert result["reason"] == "requires_attested_human_decision"


def test_the_refusal_is_short_enough_to_speak(one_blocking):
    spoken = relay.request_risk_acceptance("a1")["spoken_response"]
    assert len(spoken) <= SPEECH_CHAR_BUDGET
    assert "written reason" in spoken.lower()


def test_refusal_routes_to_a_surface_where_the_decision_can_be_made(one_blocking, monkeypatch):
    monkeypatch.setattr(relay, "CONSOLE_URL", "https://ship.example")
    assert relay.request_risk_acceptance("a1")["console_url"].startswith("https://ship.example/dashboard")


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


def test_blocking_findings_are_prioritised_over_volume():
    urgent = _status([
        _alert(repo="a/one", pr=1, severity="review", score=6.0, created="2026-09-24T01:00:00+00:00", alert_id="x1"),
        _alert(repo="a/one", pr=1, severity="review", score=6.0, created="2026-09-24T01:01:00+00:00", alert_id="x2"),
        _alert(repo="b/two", pr=2, severity="blocking", score=9.0, created="2026-09-24T02:00:00+00:00", alert_id="y1"),
    ]).most_urgent
    # One stopped merge outranks two advisory flags.
    assert urgent.label == "b/two#2"


def test_missing_finding_is_reported_not_faked(one_blocking):
    assert relay.finding_detail("does-not-exist")["found"] is False


# --- Lambda execution model: the actual bug found and fixed 2026-09-24 ---


def test_create_app_returns_a_new_instance_every_call():
    # Regression test for the real bug: the MCP SDK's StreamableHTTP
    # session manager can only run its lifespan once per instance. A
    # module-level app object reused across Lambda invocations crashed on
    # the SECOND request with "SessionManager .run() can only be called
    # once per instance" — reproduced locally before this fix, not
    # assumed. create_app() must hand back a genuinely fresh, never-run
    # instance every time it is called, not the same object memoized.
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
