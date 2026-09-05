"""
Single source of truth for which taxonomy IDs SHIP actually detects, which
regulation source file(s) ground each one, and which Screener trigger
bucket(s) can indicate each one.

Built 2026-09-05 specifically to prevent a repeat of architecture-review
finding #29/#38: the deployed AgentCore Diagnostician silently drifted out
of sync with the in-process one (stale prompt, missing corpus files) for a
full day with no error anywhere, because nothing enforced that three things
stay consistent: SCREENER_TRIGGERS' buckets, Diagnostician's taxonomy_id
values, and what's actually grounded in rag_corpus/. A human review caught
it a day later; that's not good enough for something that silently
disables detectors with zero error.

See tests/test_taxonomy_consistency.py — that test is what SHOULD have
caught #29 automatically the moment it happened, checked on every test run
from now on (including CI, if/when this project gets one), instead of
depending on someone remembering to run a manual multi-agent review.

Deliberately NOT trying to make this the actual runtime source of
SCREENER_TRIGGERS or the SYSTEM_PROMPT text — those still live where they
always did, since Screener's triggers and Diagnostician's prompt prose
serve different purposes (fast regex matching vs. natural-language
instruction to an LLM) and forcing them through one generated
representation would make both harder to read for the sake of a DRY-ness
that isn't actually needed. This registry's job is narrower and more
valuable: give an automated test something concrete to check both against.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TaxonomyEntry:
    taxonomy_id: str
    screener_buckets: tuple[str, ...]  # which SCREENER_TRIGGERS keys can indicate this ID
    corpus_files: tuple[str, ...]  # required rag_corpus/-relative paths grounding this ID
    is_statutory: bool  # False for OWASP-style industry-standard grounding (TLGP-001)


# Every ID Diagnostician's SYSTEM_PROMPT currently covers (both the
# in-process src/agents/diagnostician.py copy and the deployed
# shipagentcore/app/ship_diagnostician/main.py copy — they must match).
REGISTRY: tuple[TaxonomyEntry, ...] = (
    TaxonomyEntry("PIIE-001", ("imports", "egress", "pii_data"), ("gdpr/article_32.txt",), is_statutory=True),
    TaxonomyEntry("PIIE-002", ("logging_sinks", "pii_data"), ("gdpr/article_32.txt",), is_statutory=True),
    TaxonomyEntry("PIIE-003", ("cache_sinks", "pii_data"), ("gdpr/article_32.txt",), is_statutory=True),
    TaxonomyEntry("TLGP-002", ("decision_mutation",), ("eu_ai_act/article_14.txt",), is_statutory=True),
    TaxonomyEntry(
        "ALBP-001", ("bias_data",),
        ("eu_ai_act/article_10.txt", "eu_ai_act/annex_iii_section5.txt"), is_statutory=True,
    ),
    TaxonomyEntry(
        "TLGP-001", ("agentic",),
        ("owasp_llm_top10/llm06_excessive_agency.txt",), is_statutory=False,
    ),
)

ACTIVE_TAXONOMY_IDS: tuple[str, ...] = tuple(e.taxonomy_id for e in REGISTRY)

# Roadmap items — deliberately NOT in REGISTRY, so the consistency test
# doesn't demand grounding/prompt coverage for something not built yet.
# Kept here so "why isn't X in the registry" has one documented answer
# instead of needing to be re-derived from the roadmap/decision log.
RULED_OUT = {
    "DPSL-001": "redundant with PIIE-001",
    "DPSL-002": "not detectable from a single code diff — needs infra/deployment context",
    "DPSL-003": "not detectable from a single code diff — needs infra/deployment context",
    "TLGP-003": "not detectable from a single code diff — needs multi-file/temporal observation",
    "ALBP-002": "roadmap, not built",
    "ALBP-003": "roadmap, not built",
}
