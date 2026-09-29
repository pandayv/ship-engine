"""
Tests for the learned-pattern store — Detector's self-improvement loop.

Two things matter most: patterns below MIN_SUPPORT never reach retrieval
(the automatic guard that replaces a human approval gate, see the module
docstring for why that gate was rejected), and the retrieval math actually
ranks by real similarity rather than table order.
"""

import numpy as np
import pytest

from src.rag import pattern_store
from src.rag.pattern_store import CURSOR_ID, MIN_SUPPORT, LearnedPattern, PatternStore


class _FakeTable:
    def __init__(self, rows=None):
        self.rows = rows or {}

    def get_item(self, Key):
        row = self.rows.get(Key["pattern_id"])
        return {"Item": row} if row else {}

    def put_item(self, Item):
        self.rows[Item["pattern_id"]] = Item

    def scan(self, **kwargs):
        return {"Items": list(self.rows.values())}


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable()
    monkeypatch.setattr(pattern_store, "_table", lambda: fake)
    return fake


def _pattern(pattern_id="p1", support=MIN_SUPPORT, text="hashed identifiers are not PII"):
    return LearnedPattern(
        pattern_id=pattern_id, text=text, support_count=support,
        first_seen="2026-09-01T00:00:00+00:00", last_reinforced="2026-09-20T00:00:00+00:00",
        source_taxonomy_ids=("PIIE-001",),
    )


# --- the automatic support-threshold guard ---


def test_below_min_support_is_excluded_by_default(table):
    pattern_store.put_pattern(_pattern(support=MIN_SUPPORT - 1))
    assert pattern_store.list_patterns() == []


def test_at_min_support_is_included(table):
    pattern_store.put_pattern(_pattern(support=MIN_SUPPORT))
    assert len(pattern_store.list_patterns()) == 1


def test_eligible_only_false_returns_everything_for_the_miner():
    p = _pattern(support=1)
    assert p.eligible is False
    # The miner itself needs to see and reinforce sub-threshold patterns —
    # only Detector's retrieval path should ever filter them out.


def test_cursor_sentinel_never_appears_as_a_pattern(table):
    pattern_store.set_cursor("2026-09-20T00:00:00+00:00")
    pattern_store.put_pattern(_pattern())
    ids = {p.pattern_id for p in pattern_store.list_patterns(eligible_only=False)}
    assert CURSOR_ID not in ids


def test_cursor_round_trips(table):
    assert pattern_store.get_cursor() is None
    pattern_store.set_cursor("2026-09-20T00:00:00+00:00")
    assert pattern_store.get_cursor() == "2026-09-20T00:00:00+00:00"


def test_legacy_row_without_source_taxonomy_ids_still_loads(table):
    table.rows["p1"] = {
        "pattern_id": "p1", "text": "x", "support_count": MIN_SUPPORT,
        "first_seen": "a", "last_reinforced": "b",
    }
    patterns = pattern_store.list_patterns()
    assert patterns[0].source_taxonomy_ids == ()


# --- retrieval ranks by real similarity, not table order ---


def test_query_ranks_by_similarity_not_insertion_order(monkeypatch):
    # Three orthogonal-ish vectors; the query vector points closest to the
    # LAST-inserted pattern, so a correct implementation must still rank
    # it first — proves this isn't just returning insertion/table order.
    vectors = {
        "close": np.array([1.0, 0.0, 0.0]),
        "far": np.array([0.0, 1.0, 0.0]),
        "query": np.array([0.9, 0.1, 0.0]),
    }

    def fake_embed(texts, is_query=False):
        if is_query:
            return np.array([vectors["query"]])
        return np.array([vectors[t] for t in texts])

    monkeypatch.setattr("src.rag.vector_store.embed_texts", fake_embed)

    store = PatternStore()
    far = _pattern(pattern_id="far", text="far")
    close = _pattern(pattern_id="close", text="close")
    store.build([far, close])  # "far" inserted first, "close" second

    results = store.query("query", top_k=1)
    assert results[0].pattern_id == "close"


def test_query_against_an_empty_store_returns_nothing():
    assert PatternStore().query("anything") == []
