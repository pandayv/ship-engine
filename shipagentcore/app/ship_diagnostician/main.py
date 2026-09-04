"""
SHIP Diagnostician, deployed on Bedrock AgentCore Runtime.

This is the deployed counterpart of src/agents/diagnostician.py in the main
ship-engine repo — same system prompt, same RAG-grounded tool, same
structured output schema. Kept as a separate, simplified copy (Bedrock-only,
no multi-backend switch, no session/conversation caching — each invocation
is a single stateless classification, not a chat) because AgentCore's
generated project template lives in its own package/dependency tree.
"""

from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from rag.chunker import chunk_corpus
from rag.vector_store import VectorStore

app = BedrockAgentCoreApp()
log = app.logger

BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

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
    taxonomy_id: str | None = Field(default=None)
    risk_score: float | None = Field(default=None, ge=1, le=10)
    plain_english_summary: str | None = None
    citation: str | None = None
    remediation_patch: str | None = None


_store: VectorStore | None = None


def _get_store() -> VectorStore:
    global _store
    if _store is None:
        corpus_dir = Path(__file__).resolve().parent / "rag_corpus"
        chunks = chunk_corpus(corpus_dir)
        _store = VectorStore()
        _store.build(chunks)
    return _store


@tool
def retrieve_regulation_text(query: str) -> str:
    """Retrieve the most relevant real regulation text chunks for a query about
    a possible compliance violation. Always call this before citing any law."""
    results = _get_store().query(query, top_k=3)
    return "\n\n".join(f"[{chunk.article}, para {chunk.paragraph_index}] {chunk.text}" for chunk, _score in results)


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    """
    payload shape: {"fragment": "<isolated code fragment from Screener>"}
    Returns the DiagnosticianOutput fields as a plain dict — a single
    stateless call, not a streamed chat response.
    """
    log.info("Diagnostician invoked")
    # "fragment" is our real calling convention (used by the SHIP webhook via
    # direct boto3 invoke_agent_runtime); "prompt" is accepted as an alias
    # purely so `agentcore invoke --prompt "..."` works for quick manual
    # smoke tests, since the CLI has no way to send a custom payload shape.
    fragment = payload.get("fragment") or payload.get("prompt", "")
    if not fragment:
        raise ValueError("payload must include a non-empty 'fragment' (or 'prompt') string")

    agent = Agent(
        model=BedrockModel(model_id=BEDROCK_MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_regulation_text],
        structured_output_model=DiagnosticianOutput,
    )
    result = agent(f"Isolated code fragment flagged by Screener:\n\n{fragment}")
    return result.structured_output.model_dump()


if __name__ == "__main__":
    app.run()
