"""
Diagnostician — SHIP's semantic evaluator (formerly "Tier 2"). Runs only on
fragments Screener has already flagged. A Strands Agent on Amazon Bedrock,
RAG-grounded against real regulation text (never unaided model recollection).

NOT LIVE-TESTED as of this writing — requires AWS credentials (Bedrock
access) that aren't configured in this environment. Code is written to the
real Strands + Bedrock API (verified against the installed strands-agents
1.54.0 package's actual signatures, not guessed), ready to run once AWS
Builder ID / account setup is done.

IMPORTANT scoping note (per ship_roadmap.md): this prompt is deliberately
narrowed to PIIE-001 only, NOT the full 12-flag taxonomy from the original
draft. Asking Diagnostician to judge categories we haven't sourced RAG text
for (ALBP, TLGP, DPSL) would risk exactly the hallucinated-citation failure
the "Trust but Verify" design principle exists to prevent. Widen this only
when a second detector's grounding corpus actually exists — see roadmap's
feature-flag rollout model.
"""

from pathlib import Path

from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from src.rag.chunker import chunk_corpus
from src.rag.vector_store import VectorStore

SYSTEM_PROMPT = """You are the SHIP Diagnostician, a compliance auditor for a small \
fintech startup's engineering team. You analyze a single isolated code fragment \
that has already been flagged by a fast keyword pre-filter (Screener) as \
POTENTIALLY containing a PIIE-001 violation: raw, un-anonymized personal \
identifiers (name, SSN, date of birth, income, account number, blood group, \
etc.) being sent to an external LLM's prompt context without tokenization or \
hashing.

Screener's keyword match is a POTENTIAL signal, not a confirmed violation — \
your job is to judge whether it's real. A variable named `email` that is \
never actually sent anywhere, or PII that IS already masked/hashed before \
use, is NOT a violation and must not be flagged as one.

You MUST use the `retrieve_regulation_text` tool to ground your reasoning in \
the actual GDPR Article 32 text before making any determination — never cite \
a regulation from memory alone.

If you confirm a real PIIE-001 violation:
- Assign a risk_score from 1-10. A score >= 7.5 means raw, directly-identifying \
  financial PII (SSN, account number, full name + income together) reaching an \
  external, closed-source model with no masking at all. Lower scores are for \
  partial/ambiguous cases.
- Write a plain_english_summary a non-technical founder could understand in one \
  read.
- Provide the exact citation (article and paragraph) from the retrieved text — \
  never paraphrase the paragraph number, quote what the tool actually returned.
- Draft a remediation_patch: a small, concrete code change (e.g. redact or hash \
  the field before it's included in the prompt) that would resolve the issue.

If Screener's match was a false positive, set matched=false and leave the other \
fields empty — do not invent a violation to justify the escalation."""


class DiagnosticianOutput(BaseModel):
    matched: bool = Field(description="True only if a real PIIE-001 violation was confirmed, not just Screener's keyword match")
    taxonomy_id: str | None = Field(default=None, description="e.g. 'PIIE-001'")
    risk_score: float | None = Field(default=None, ge=1, le=10)
    plain_english_summary: str | None = None
    citation: str | None = Field(default=None, description="Exact article/paragraph from the retrieved regulation text")
    remediation_patch: str | None = None


_store: VectorStore | None = None


def _get_store() -> VectorStore:
    global _store
    if _store is None:
        corpus_dir = Path(__file__).resolve().parents[2] / "rag_corpus"
        chunks = chunk_corpus(corpus_dir)
        _store = VectorStore()
        _store.build(chunks)  # calls Bedrock Titan embeddings — needs AWS credentials
    return _store


@tool
def retrieve_regulation_text(query: str) -> str:
    """Retrieve the most relevant real regulation text chunks for a query about
    a possible compliance violation. Always call this before citing any law."""
    results = _get_store().query(query, top_k=3)
    return "\n\n".join(f"[{r.chunk.article}, para {r.chunk.paragraph_index}] {r.chunk.text}" for r in results)


DIAGNOSTICIAN_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# "us." prefix = cross-region inference profile, required for on-demand use
# of this model (raw model ID alone gets a ValidationException from Bedrock).
# Right-sized for the task: Screener has already pre-filtered the input, so
# Diagnostician's job is bounded classification + structured drafting, not
# open-ended reasoning. Current-gen Haiku (not the older Claude 3 Haiku also
# available) is meaningfully cheaper/faster than Sonnet/Opus for this. If
# testing shows it's missing nuance on real violations, swap to a Sonnet ID
# from `aws bedrock list-foundation-models --by-provider anthropic` — one
# line to change, no architecture impact either way.


def build_agent() -> Agent:
    model = BedrockModel(model_id=DIAGNOSTICIAN_MODEL_ID)
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_regulation_text],
        structured_output_model=DiagnosticianOutput,
    )


def diagnose(isolated_fragment: str) -> DiagnosticianOutput:
    """Entry point Triage/the webhook route call. Needs AWS credentials configured."""
    agent = build_agent()
    result = agent(f"Isolated code fragment flagged by Screener:\n\n{isolated_fragment}")
    return result.structured_output  # type: ignore[return-value]


if __name__ == "__main__":
    # Will only run once AWS credentials are configured (Bedrock + the
    # embedding calls inside retrieve_regulation_text both need them).
    from src.agents.screener import isolate_fragment, scan

    bad_sample = '''
import openai

def get_underwriting_opinion(client):
    prompt = f"Applicant {client.first_name} {client.last_name}, DOB {client.date_of_birth}, " \\
             f"income {client.annual_income}. Should we approve?"
    return openai.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
'''
    screener_result = scan(bad_sample)
    fragment = isolate_fragment(bad_sample, screener_result.matched_terms)
    print("Screener isolated fragment, handing to Diagnostician:\n", fragment)
    verdict = diagnose(fragment)
    print(verdict.model_dump_json(indent=2))
