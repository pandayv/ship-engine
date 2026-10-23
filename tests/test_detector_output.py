"""
Pure-logic tests for DetectorOutput's validation — no AWS credentials
needed, since these test pydantic validation and Python control flow, not
any live model call.
"""

import pytest
from pydantic import ValidationError

from src.agents.detector import DetectorOutput


def test_matched_false_allows_all_fields_empty():
    # a dismissed false positive shouldn't need any of the other fields
    result = DetectorOutput(matched=False)
    assert result.taxonomy_id is None


def test_matched_true_with_all_fields_populated_is_valid():
    result = DetectorOutput(
        matched=True, taxonomy_id="PIIE-001", risk_score=9.0,
        citation="GDPR Art. 32(1)(a)", plain_english_summary="Raw PII sent externally.",
    )
    assert result.matched is True


@pytest.mark.parametrize("missing_field", ["taxonomy_id", "risk_score", "citation", "plain_english_summary"])
def test_matched_true_missing_any_required_field_raises(missing_field):
    # "Matched implies all four fields populated" is enforced at construction time. A model that
    # skips the RAG tool call (Ollama has) can produce exactly this malformed shape, and it would
    # otherwise crash triage.route() several frames away from the actual cause. The error names
    # what is missing.
    fields = {
        "matched": True, "taxonomy_id": "PIIE-001", "risk_score": 9.0,
        "citation": "GDPR Art. 32(1)(a)", "plain_english_summary": "Raw PII sent externally.",
    }
    fields[missing_field] = None
    with pytest.raises(ValidationError, match=missing_field):
        DetectorOutput(**fields)
