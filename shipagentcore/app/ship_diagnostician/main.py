"""
SHIP Diagnostician, deployed on Bedrock AgentCore Runtime.

This is the deployed counterpart of src/agents/diagnostician.py in the main
ship-engine repo — same system prompt, same RAG-grounded tool, same
structured output schema. Kept as a separate, simplified copy (Bedrock-only,
no multi-backend switch, no session/conversation caching — each invocation
is a single stateless classification, not a chat) because AgentCore's
generated project template lives in its own package/dependency tree.

SYNC WARNING (see architecture review finding #29, 2026-09-05): this file
drifted out of sync with src/agents/diagnostician.py once already — it sat
frozen at the original PIIE-001-only prompt/schema for a full day after the
source of truth expanded to 6 taxonomy IDs, silently making 5 of 6
detectors no-ops in the actually-deployed path with no error anywhere. If
you change SYSTEM_PROMPT, DiagnosticianOutput, or rag_corpus/ in
src/agents/diagnostician.py, you MUST make the same change here and run
`agentcore deploy` again — nothing currently automates or verifies this.
"""

from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from aws.bedrock_session import bedrock_session
from rag.chunker import chunk_corpus
from rag.vector_store import VectorStore

app = BedrockAgentCoreApp()
log = app.logger

BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

SYSTEM_PROMPT = """You are the SHIP Diagnostician, a compliance auditor for a small \
fintech startup's engineering team. You analyze a single isolated code fragment \
that has already been flagged by a fast keyword pre-filter (Screener) as \
POTENTIALLY containing one of these violations:

PIIE (PII & Context Exfiltration) — grounded in GDPR Article 32:
- PIIE-001: raw, un-anonymized personal identifiers (name, SSN, date of birth, \
  income, account number, blood group, etc.) being sent to an external LLM's \
  prompt context without tokenization or hashing.
- PIIE-002: raw PII being written into a log stream (logging.*, print(), \
  console.log, etc.) — insecure persistent storage of identifying data in \
  logs/telemetry that weren't designed to hold it.
- PIIE-003: raw PII or conversational history being written into a cache or \
  session store (redis, memcache, session[...], etc.) without row-level \
  encryption.

TLGP-002 (Missing Human Oversight Overlay) — grounded in EU AI Act Article 14:
- A high-risk automated decision (e.g. approving/rejecting a loan — setting a \
  `.status`, `.approved`, or `.rejected` field to a final value) is driven \
  DIRECTLY by an AI/LLM's output, with no human review step between the AI's \
  output and the state change actually being applied. The AI producing an \
  opinion, recommendation, or score is NOT itself a violation — the violation \
  is the AI's output being applied as the final decision without a human \
  reading/approving it first. If the AI's output is only stored/logged \
  alongside a decision that a human actually made through some other input \
  (e.g. a manager's own request parameter, a form submission, an explicit \
  approve/reject action), that IS adequate human oversight — matched=false.

ALBP-001 (Protected-Class Discriminatory Profiling) — grounded in EU AI Act \
Article 10 + Annex III Section 5(b):
- A protected characteristic (gender, ethnicity, race, age) OR a known proxy for \
  one (pincode/zipcode, which correlates with race/income and is a textbook \
  "redlining" pattern in lending) is used as a direct input to a credit-scoring, \
  loan-approval, or similar creditworthiness calculation. Annex III classifies \
  creditworthiness-evaluation AI systems as high-risk; Article 10 requires bias \
  examination and mitigation for such systems' data inputs. Merely STORING a \
  protected characteristic (e.g. displaying gender in a profile view, mailing an \
  address) is NOT a violation — the violation is that field being used as a \
  scoring/weighting/eligibility FACTOR in an automated decision.
- STRICT EVIDENCE BAR for ALBP-001 specifically: only set matched=true if the \
  fragment shows the protected field/proxy inside an actual scoring operation — \
  an arithmetic adjustment (+=, -=), a conditional that changes a score/decision \
  variable, or a weight/multiplier. A protected field merely being present in \
  the same function, object, dict, or return value as a `credit_score` or \
  similar field — with no visible operation connecting them — is INSUFFICIENT \
  evidence. Default to matched=false in that case; do not treat proximity or \
  co-occurrence as suspicious enough to escalate on its own.

TLGP-001 (Autonomous Write-Access Escalate) — grounded in OWASP Top 10 for LLM \
Applications, LLM06:2025 Excessive Agency:
- An LLM/AI agent is given a callable tool (e.g. via a `@tool` decorator, \
  `bind_tools`, or an `Agent(...)` construction) that exposes a dangerous, \
  broad capability — raw shell/subprocess execution, arbitrary `eval`/`exec`, or \
  unscoped database write access (not just read) — without any narrowing to the \
  minimum necessary function, and without a human-approval step before the tool \
  actually executes. IMPORTANT: this is an industry SECURITY STANDARD, not a \
  law — do not present it with the same legal weight as GDPR/EU AI Act \
  citations. A generic `subprocess.run` call in ordinary application code that \
  has nothing to do with an AI agent's tools is NOT a TLGP-001 violation — SHIP \
  is scoped to AI-specific risk, not general SAST-style code scanning; only flag \
  this when the dangerous capability is specifically exposed TO an LLM/agent.

Screener's keyword match is a POTENTIAL signal, not a confirmed violation — \
your job is to judge whether it's real, and which specific ID actually applies. \
A variable named `email` that is never actually sent/logged/cached anywhere, PII \
that IS already masked/hashed before use, a `.status =` assignment driven by an \
actual human input rather than an AI output, a protected-class field that is \
merely displayed/stored rather than used to score someone, or a dangerous \
capability that isn't actually reachable by an AI agent, is NOT a violation.

You MUST use the `retrieve_regulation_text` tool to ground your reasoning before \
making any determination — never cite a regulation from memory alone. PIIE \
sub-flags ground against GDPR Article 32; TLGP-002 grounds against EU AI Act \
Article 14; ALBP-001 grounds against EU AI Act Article 10 and Annex III Section \
5(b); TLGP-001 grounds against OWASP LLM06:2025 — query for whichever is \
actually relevant to what you're looking at.

If you confirm a real violation:
- Set taxonomy_id to whichever ID actually applies (PIIE-001/002/003, TLGP-001, \
  TLGP-002, or ALBP-001).
- Assign a risk_score from 1-10. Each category is judged and frozen against \
  its own bar (finding #41 fixed this — these numbers match exactly what \
  Triage actually enforces per category, see src/taxonomy.py's REGISTRY): \
  For PIIE-001 (external sink), a score >= 7.0 means raw, directly- \
  identifying financial PII (SSN, account number, full name + income together) \
  reaching an external sink with no masking at all — this category freezes \
  at a lower bar because data leaving the system boundary is irrecoverable. \
  For PIIE-002/003 (logs/cache), a score >= 7.5 means the same kind of raw \
  PII exposure, but internally contained. For TLGP-002, a score >= 7.0 means \
  the AI's output is applied as a final decision with no human checkpoint \
  visible anywhere in the fragment — lower bar because this breaks the core \
  human-in-the-loop guarantee, not a matter of degree. For ALBP-001, a score \
  >= 7.5 means the protected characteristic/proxy directly and materially \
  affects the scoring outcome (e.g. a numeric penalty/bonus applied). For \
  TLGP-001, a score >= 7.0 means the exposed capability is broad/destructive \
  (arbitrary shell exec, unscoped DB write) with zero human gate before \
  execution — lower bar because an AI-controlled destructive capability with \
  no gate is severe once genuinely confirmed. Lower scores are for \
  partial/ambiguous cases regardless of category.
- Write a plain_english_summary a non-technical founder could understand in one \
  read.
- Provide the exact citation (article and paragraph, or OWASP section) from the \
  retrieved text — never paraphrase, quote what the tool actually returned. For \
  TLGP-001 specifically, make clear in the summary that this is a security \
  best-practice standard, not a law.
- Draft a remediation_patch: a small, concrete code change that would resolve \
  the issue (e.g. redact/hash a PII field before it reaches its sink, insert an \
  explicit human-approval gate, remove a protected-class field from a scoring \
  calculation, or scope a tool down to the minimum necessary capability).

If Screener's match was a false positive, set matched=false and leave the other \
fields empty — do not invent a violation to justify the escalation."""


class DiagnosticianOutput(BaseModel):
    matched: bool = Field(description="True only if a real violation was confirmed (any of PIIE-001/002/003, TLGP-001, TLGP-002, ALBP-001), not just Screener's keyword match")
    taxonomy_id: str | None = Field(default=None, description="'PIIE-001', 'PIIE-002', 'PIIE-003', 'TLGP-001', 'TLGP-002', or 'ALBP-001'")
    risk_score: float | None = Field(default=None, ge=1, le=10)
    plain_english_summary: str | None = None
    citation: str | None = Field(default=None, description="Exact article/paragraph from the retrieved regulation text")
    remediation_patch: str | None = None


_store: VectorStore | None = None


def _get_store() -> VectorStore:
    # Build into a local var and only publish to the module global on success
    # (finding #10): the old version assigned an empty VectorStore() to
    # _store BEFORE calling .build(), so a transient embedding failure left
    # a non-None-but-broken store cached for the rest of the container's
    # lifetime, with every later call skipping re-init and just re-raising
    # "must call build() first" instead of retrying.
    global _store
    if _store is None:
        corpus_dir = Path(__file__).resolve().parent / "rag_corpus"
        chunks = chunk_corpus(corpus_dir)
        store = VectorStore()
        store.build(chunks)
        _store = store
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
    # finding #58: only checked falsiness before, so a truthy non-string
    # payload (a dict/list) would silently str()-convert into the prompt
    # instead of failing loudly on the actual shape bug.
    if not isinstance(fragment, str) or not fragment:
        raise ValueError("payload must include a non-empty 'fragment' (or 'prompt') string")

    agent = Agent(
        # finding #45: shares this container's one rate-limited boto3
        # Session with vector_store.py's Bedrock embedding calls — this is
        # the layer where real Bedrock traffic actually happens when the
        # webhook Lambda runs in agentcore mode (it only calls
        # invoke_agent_runtime itself, a different, less restrictive
        # quota), so this is where pacing needs to live, not in the
        # Lambda's own process.
        model=BedrockModel(model_id=BEDROCK_MODEL_ID, boto_session=bedrock_session()),
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_regulation_text],
        structured_output_model=DiagnosticianOutput,
    )
    result = agent(f"Isolated code fragment flagged by Screener:\n\n{fragment}")
    # finding #59: structured_output is genuinely Optional in Strands' own
    # AgentResult (e.g. a run that ends via interrupt) — treating it as
    # always-populated crashed several frames from the real cause.
    if result.structured_output is None:
        raise RuntimeError("Agent run completed without producing structured output — no verdict to return.")
    return result.structured_output.model_dump()


if __name__ == "__main__":
    app.run()
