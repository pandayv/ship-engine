"""
Tests for the connected-repositories store.

Most of these are about is_watched(), which is not a convenience lookup —
it is the control that stops a forged webhook payload naming an arbitrary
repository and spending this service's GitHub token and Bedrock quota
against it (review finding #7). The behaviour that matters most is what it
does when the datastore is unavailable, because the tempting answer there
is the one that silently removes the control.
"""

import pytest

from src.storage import repo_store
from src.storage.repo_store import STATUS_AWAITING, STATUS_WATCHING, WatchedRepo


class _FakeTable:
    """Minimal stand-in for the DynamoDB Table calls this module makes."""

    def __init__(self, rows=None, raises=None):
        self.rows = rows or {}
        self.raises = raises
        self.deleted = []

    def _boom(self):
        if self.raises:
            raise self.raises

    def get_item(self, Key):
        self._boom()
        row = self.rows.get(Key["repo_full_name"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self._boom()
        self.rows[Item["repo_full_name"]] = Item

    def delete_item(self, Key):
        self._boom()
        self.deleted.append(Key["repo_full_name"])
        self.rows.pop(Key["repo_full_name"], None)

    def scan(self, **kwargs):
        self._boom()
        return {"Items": list(self.rows.values())}

    def update_item(self, Key, **kwargs):
        self._boom()
        name = Key["repo_full_name"]
        if name not in self.rows:
            raise _ConditionalCheckFailedException("no such row")
        self.rows[name]["status"] = STATUS_WATCHING
        self.rows[name]["last_event_at"] = "2026-09-08T00:00:00+00:00"


class _ConditionalCheckFailedException(Exception):
    pass


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(repo_store, "_table", lambda: fake)
    monkeypatch.setattr(repo_store, "BOOTSTRAP_REPOS", ())
    return fake


def _row(name, status=STATUS_AWAITING):
    return {"repo_full_name": name, "status": status, "connected_at": "2026-09-08T00:00:00+00:00"}


# --- the allowlist control ---


def test_connected_repo_is_watched(table):
    repo_store.connect_repo("pandayv/micro-finance")
    assert repo_store.is_watched("pandayv/micro-finance") is True


def test_unknown_repo_is_not_watched(table):
    assert repo_store.is_watched("someone-else/private-repo") is False


def test_bootstrap_env_repo_is_watched_without_a_table_row(table, monkeypatch):
    # Lets local dev and an existing deployment work with no table and no
    # migration step.
    monkeypatch.setattr(repo_store, "BOOTSTRAP_REPOS", ("pandayv/micro-finance",))
    assert repo_store.is_watched("pandayv/micro-finance") is True
    assert table.rows == {}


def test_is_watched_fails_closed_when_the_datastore_errors(monkeypatch):
    # THE important one. If DynamoDB is unreachable, denying is the only
    # safe answer: returning True on error would delete the allowlist
    # precisely when the system is least healthy.
    monkeypatch.setattr(repo_store, "_table", lambda: _FakeTable(raises=RuntimeError("dynamodb down")))
    monkeypatch.setattr(repo_store, "BOOTSTRAP_REPOS", ())
    assert repo_store.is_watched("pandayv/micro-finance") is False


def test_datastore_error_still_honours_the_bootstrap_list(monkeypatch):
    # Fails closed, but not closed-and-broken: a repo explicitly named in
    # configuration stays allowed without the table being readable at all,
    # because that check never touches the datastore.
    monkeypatch.setattr(repo_store, "_table", lambda: _FakeTable(raises=RuntimeError("dynamodb down")))
    monkeypatch.setattr(repo_store, "BOOTSTRAP_REPOS", ("pandayv/micro-finance",))
    assert repo_store.is_watched("pandayv/micro-finance") is True


# --- connect / disconnect ---


def test_connect_starts_in_awaiting_first_event(table):
    repo = repo_store.connect_repo("pandayv/micro-finance", connected_by="Product Reviewer")
    assert repo.status == STATUS_AWAITING
    assert repo.connected_by == "Product Reviewer"


def test_reconnecting_does_not_reset_a_live_repo_to_awaiting(table):
    # A repo already receiving deliveries must not be knocked back to
    # "awaiting first event" by someone re-submitting the connect form —
    # that would make a working integration look broken.
    table.rows["pandayv/micro-finance"] = _row("pandayv/micro-finance", status=STATUS_WATCHING)
    assert repo_store.connect_repo("pandayv/micro-finance").status == STATUS_WATCHING


def test_disconnect_removes_the_repo(table):
    repo_store.connect_repo("pandayv/micro-finance")
    repo_store.disconnect_repo("pandayv/micro-finance")
    assert repo_store.is_watched("pandayv/micro-finance") is False


def test_list_repos_is_ordered_by_when_they_were_connected(table):
    table.rows = {
        "b/second": {"repo_full_name": "b/second", "status": STATUS_AWAITING, "connected_at": "2026-09-08T02:00:00+00:00"},
        "a/first": {"repo_full_name": "a/first", "status": STATUS_AWAITING, "connected_at": "2026-09-08T01:00:00+00:00"},
    }
    assert [r.repo_full_name for r in repo_store.list_repos()] == ["a/first", "b/second"]


def test_legacy_row_without_newer_fields_still_loads(table):
    # Same schema-evolution tolerance the alert store needed.
    table.rows["a/b"] = {"repo_full_name": "a/b", "status": STATUS_AWAITING, "connected_at": "x"}
    assert repo_store.list_repos() == [WatchedRepo(repo_full_name="a/b", status=STATUS_AWAITING, connected_at="x")]


# --- first-delivery transition ---


def test_first_delivery_flips_awaiting_to_watching(table):
    repo_store.connect_repo("pandayv/micro-finance")
    repo_store.mark_event_seen("pandayv/micro-finance")
    assert repo_store.get_repo("pandayv/micro-finance").status == STATUS_WATCHING


def test_marking_a_bootstrap_only_repo_does_not_create_a_row(table, monkeypatch):
    # The configured bootstrap list and the connected list stay distinct —
    # a delivery must not quietly migrate one into the other.
    monkeypatch.setattr(repo_store, "BOOTSTRAP_REPOS", ("pandayv/micro-finance",))
    repo_store.mark_event_seen("pandayv/micro-finance")
    assert table.rows == {}


def test_marking_never_raises_even_if_the_datastore_is_down(monkeypatch):
    # This is dashboard presentation. Failing to record it must never fail
    # the webhook delivery it is describing.
    monkeypatch.setattr(repo_store, "_table", lambda: _FakeTable(raises=RuntimeError("dynamodb down")))
    repo_store.mark_event_seen("pandayv/micro-finance")  # must not raise
