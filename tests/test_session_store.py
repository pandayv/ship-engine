"""
Tests for session_store — the "was this alert shown recently" signal
request_risk_acceptance depends on. Keyed by alert_id and a time window,
not by MCP session — see the module's own docstring for why: Relay's
create_app() builds a brand-new session manager on every Lambda
invocation, so there is no session continuity to key on, confirmed live
against the actually-deployed system after an earlier session-based
version of this store shipped and silently never worked.

The two properties that matter: an unshown alert never reads as shown,
and a datastore error fails CLOSED (reads as not-reviewed) — the unsafe
failure direction here is a blind approval slipping through, not an
unnecessary redirect to Gate, same standard as repo_store.is_watched().
"""

import pytest

from src.storage import session_store


class _FakeTable:
    def __init__(self, rows=None, raises=None):
        self.rows = rows or {}
        self.raises = raises

    def _boom(self):
        if self.raises:
            raise self.raises

    def get_item(self, Key):
        self._boom()
        row = self.rows.get(Key["alert_id"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self._boom()
        self.rows[Item["alert_id"]] = Item


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(session_store, "_table", lambda: fake)
    return fake


def test_unshown_alert_reads_as_not_shown(table):
    assert session_store.was_shown_recently("a1") is False


def test_marking_then_checking_reads_as_shown(table):
    session_store.mark_shown("a1")
    assert session_store.was_shown_recently("a1") is True


def test_marking_one_alert_does_not_mark_another(table):
    session_store.mark_shown("a1")
    assert session_store.was_shown_recently("a2") is False


def test_a_review_past_the_window_no_longer_counts(table):
    session_store.mark_shown("a1")
    table.rows["a1"]["shown_at"] -= session_store.RECENT_WINDOW_SECONDS + 1
    assert session_store.was_shown_recently("a1") is False


def test_a_review_just_inside_the_window_still_counts(table):
    session_store.mark_shown("a1")
    table.rows["a1"]["shown_at"] -= session_store.RECENT_WINDOW_SECONDS - 1
    assert session_store.was_shown_recently("a1") is True


def test_re_showing_refreshes_the_window(table):
    session_store.mark_shown("a1")
    table.rows["a1"]["shown_at"] -= session_store.RECENT_WINDOW_SECONDS + 1
    assert session_store.was_shown_recently("a1") is False

    session_store.mark_shown("a1")  # shown again, now
    assert session_store.was_shown_recently("a1") is True


def test_was_shown_recently_fails_closed_on_a_datastore_error(monkeypatch):
    monkeypatch.setattr(session_store, "_table", lambda: _FakeTable(raises=RuntimeError("dynamodb down")))
    assert session_store.was_shown_recently("a1") is False


def test_mark_shown_never_raises_even_if_the_datastore_is_down(monkeypatch):
    monkeypatch.setattr(session_store, "_table", lambda: _FakeTable(raises=RuntimeError("dynamodb down")))
    session_store.mark_shown("a1")  # must not raise
