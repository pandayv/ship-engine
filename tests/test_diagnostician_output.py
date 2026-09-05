"""
Pure-logic tests for DiagnosticianOutput's validation — no AWS credentials
needed, since these test pydantic validation and Python control flow, not
any live model call. Added 2026-09-05: these fixes (the model_validator,
the singleton-poisoning fix) had been written but never actually verified
by a test — this closes that gap.
"""

import pytest
from pydantic import ValidationError

from src.agents.diagnostician import DiagnosticianOutput


def test_matched_false_allows_all_fields_empty():
    # a dismissed false positive shouldn't need any of the other fields
    result = DiagnosticianOutput(matched=False)
    assert result.taxonomy_id is None


def test_matched_true_with_all_fields_populated_is_valid():
    result = DiagnosticianOutput(
        matched=True, taxonomy_id="PIIE-001", risk_score=9.0,
        citation="GDPR Art. 32(1)(a)", plain_english_summary="Raw PII sent externally.",
    )
    assert result.matched is True


@pytest.mark.parametrize("missing_field", ["taxonomy_id", "risk_score", "citation", "plain_english_summary"])
def test_matched_true_missing_any_required_field_raises(missing_field):
    # review findings #46/#51: this used to be enforced only by prose in
    # the prompt — a real observed failure mode (Ollama skipping the RAG
    # tool call) could produce exactly this malformed shape and crash
    # triage.route() several frames away from the actual cause. Now it's
    # rejected at construction time, with a clear message naming what's
    # missing.
    fields = {
        "matched": True, "taxonomy_id": "PIIE-001", "risk_score": 9.0,
        "citation": "GDPR Art. 32(1)(a)", "plain_english_summary": "Raw PII sent externally.",
    }
    fields[missing_field] = None
    with pytest.raises(ValidationError, match=missing_field):
        DiagnosticianOutput(**fields)
