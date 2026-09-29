"""
Tests for the pattern-mining batch job (Detector's self-improvement loop).

The two things that actually matter: incremental behavior (never
reprocess a dismissal the cursor already passed) and the
reinforce-vs-create branch, since a bug there either silently duplicates
near-identical patterns forever or never lets a pattern reach MIN_SUPPORT.
Both AWS boundaries (the Bedrock extraction call, the embedding calls
inside PatternStore) are mocked — this tests the mining logic, not Bedrock
itself.
"""

import numpy as np
import pytest

import scripts.pattern_miner as miner
from src.rag import pattern_store
from src.rag.pattern_store import LearnedPattern
from src.storage.alert_store import Alert


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


def _alert(alert_id="a1", taxonomy_id="PIIE-001", resolution="rejected",
           resolved_at="2026-09-27T00:00:00+00:00", reason="hashed before sending, not raw PII"):
    return Alert(
        alert_id=alert_id, repo="pandayv/micro-finance", pr_number=1, file="x.py",
        taxonomy_id=taxonomy_id, risk_score=6.0, plain_english_summary="sends an identifier externally",
        citation="GDPR Art. 32", remediation_patch="", status="resolved",
        created_at="2026-09-26T00:00:00+00:00", resolution=resolution,
        resolved_at=resolved_at, resolution_reason=reason,
    )


def _mock_extraction(monkeypatch, text="hashed identifiers are not PII"):
    monkeypatch.setattr(miner, "_extract_pattern_text", lambda alert: text)


def _mock_similarity(monkeypatch, value):
    """Forces _find_reinforceable_pattern's similarity check to a fixed
    value, independent of the embedding math (already covered directly by
    test_pattern_store.py's own similarity-ranking test)."""
    monkeypatch.setattr(
        miner, "_find_reinforceable_pattern",
        lambda candidate_text, existing: (existing[0] if existing and value >= miner.SIMILARITY_THRESHOLD else None),
    )


# --- only real dismissals are mined ---


def test_approved_resolutions_are_never_mined(table, monkeypatch):
    _mock_extraction(monkeypatch)
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert(resolution="approved")],
    )
    result = miner.mine()
    assert result["processed"] == 0


# --- incremental behavior ---


def test_first_run_with_no_cursor_processes_everything(table, monkeypatch):
    _mock_extraction(monkeypatch)
    _mock_similarity(monkeypatch, value=0.0)
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert(alert_id="a1", resolved_at="2026-09-27T00:00:00+00:00")],
    )
    result = miner.mine()
    assert result["processed"] == 1
    assert pattern_store.get_cursor() == "2026-09-27T00:00:00+00:00"


def test_a_second_run_only_processes_dismissals_after_the_cursor(table, monkeypatch):
    _mock_extraction(monkeypatch)
    _mock_similarity(monkeypatch, value=0.0)
    pattern_store.set_cursor("2026-09-27T00:00:00+00:00")
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [
            _alert(alert_id="old", resolved_at="2026-09-26T00:00:00+00:00"),  # already mined
            _alert(alert_id="new", resolved_at="2026-09-28T00:00:00+00:00"),  # not yet mined
        ],
    )
    result = miner.mine()
    assert result["processed"] == 1  # only "new", never re-processes "old"


# --- reinforce vs. create ---


def test_a_close_match_reinforces_instead_of_duplicating(table, monkeypatch):
    existing = LearnedPattern(
        pattern_id="p1", text="hashed identifiers are not PII", support_count=1,
        first_seen="2026-09-01T00:00:00+00:00", last_reinforced="2026-09-01T00:00:00+00:00",
        source_taxonomy_ids=("PIIE-001",),
    )
    pattern_store.put_pattern(existing)
    _mock_extraction(monkeypatch)
    _mock_similarity(monkeypatch, value=0.9)  # above SIMILARITY_THRESHOLD
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert()],
    )

    result = miner.mine()

    assert result == {"processed": 1, "reinforced": 1, "created": 0}
    stored = pattern_store.list_patterns(eligible_only=False)
    assert len(stored) == 1  # no duplicate row
    assert stored[0].support_count == 2


def test_no_close_match_creates_a_new_pattern_at_support_one(table, monkeypatch):
    _mock_extraction(monkeypatch, text="a genuinely new pattern")
    _mock_similarity(monkeypatch, value=0.0)  # below SIMILARITY_THRESHOLD
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert()],
    )

    result = miner.mine()

    assert result == {"processed": 1, "reinforced": 0, "created": 1}
    stored = pattern_store.list_patterns(eligible_only=False)
    assert len(stored) == 1
    assert stored[0].support_count == 1


def test_a_new_pattern_is_not_yet_eligible_for_retrieval(table, monkeypatch):
    # support_count == 1 < MIN_SUPPORT — proves the automatic guard
    # actually applies to what this script writes, not just in isolation.
    _mock_extraction(monkeypatch)
    _mock_similarity(monkeypatch, value=0.0)
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert()],
    )
    miner.mine()
    assert pattern_store.list_patterns(eligible_only=True) == []


def test_reinforcement_merges_a_second_taxonomy_id(table, monkeypatch):
    existing = LearnedPattern(
        pattern_id="p1", text="hashed identifiers are not PII", support_count=1,
        first_seen="2026-09-01T00:00:00+00:00", last_reinforced="2026-09-01T00:00:00+00:00",
        source_taxonomy_ids=("PIIE-001",),
    )
    pattern_store.put_pattern(existing)
    _mock_extraction(monkeypatch)
    _mock_similarity(monkeypatch, value=0.9)
    monkeypatch.setattr(
        "src.storage.alert_store.list_resolved_alerts",
        lambda: [_alert(taxonomy_id="PIIE-002")],
    )

    miner.mine()

    stored = pattern_store.list_patterns(eligible_only=False)[0]
    assert set(stored.source_taxonomy_ids) == {"PIIE-001", "PIIE-002"}


def test_no_new_dismissals_leaves_the_cursor_untouched(table, monkeypatch):
    pattern_store.set_cursor("2026-09-27T00:00:00+00:00")
    monkeypatch.setattr("src.storage.alert_store.list_resolved_alerts", lambda: [])
    miner.mine()
    assert pattern_store.get_cursor() == "2026-09-27T00:00:00+00:00"
