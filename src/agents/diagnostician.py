"""
Diagnostician — SHIP's semantic evaluator (formerly "Tier 2"). Runs only on
fragments Screener has already flagged. A Strands Agent, RAG-grounded
against real regulation text (never unaided model recollection).

Model backend is switchable via SHIP_MODEL_BACKEND ("bedrock" | "ollama",
default "ollama" as of 2026-09-04) — see BEDROCK_MODEL_ID/OLLAMA_MODEL_ID
below. Bedrock is the intended AWS-native choice for the real submission,
but the account is currently blocked at a 0.0 default quota across every
model, first-party and third-party alike (confirmed via real Service
Quotas data — not something a different Bedrock model sidesteps). Ollama
runs fully local for POC/dev purposes in the meantime.

IMPORTANT scoping note (per ship_roadmap.md): this prompt covers PIIE-001/
002/003 (GDPR Art. 32), TLGP-002 (EU AI Act Art. 14), ALBP-001 (EU AI Act
Art. 10 + Annex III Section 5(b)), and TLGP-001 (OWASP LLM06:2025 Excessive
Agency — an industry security standard, not a law; Diagnostician must be
honest about that distinction in its output). Still deliberately NOT
widened to DPSL-002/003 or TLGP-003, which are structurally undetectable
from a single code diff (need infra/deployment config or multi-file/
temporal observation, not something a diff-scanning architecture can see)
regardless of grounding — see roadmap. Only widen further for a category
that's both diff-detectable AND has a sourced grounding corpus — asking
Diagnostician to judge anything else would risk the hallucinated-citation
failure "Trust but Verify" exists to prevent.
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field
from strands import Agent, tool

from src.rag.chunker import chunk_corpus
from src.rag.vector_store import VectorStore

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
- Assign a risk_score from 1-10. For PIIE, a score >= 7.5 means raw, directly- \
  identifying financial PII (SSN, account number, full name + income together) \
  reaching an external sink with no masking at all. For TLGP-002, a score >= 7.5 \
  means the AI's output is applied as a final decision with no human checkpoint \
  visible anywhere in the fragment. For ALBP-001, a score >= 7.5 means the \
  protected characteristic/proxy directly and materially affects the scoring \
  outcome (e.g. a numeric penalty/bonus applied). For TLGP-001, a score >= 7.5 \
  means the exposed capability is broad/destructive (arbitrary shell exec, \
  unscoped DB write) with zero human gate before execution. Lower scores are \
  for partial/ambiguous cases.
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
    matched: bool = Field(description="True only if a real PIIE-001 violation was confirmed, not just Screener's keyword match")
    taxonomy_id: str | None = Field(default=None, description="'PIIE-001', 'PIIE-002', 'PIIE-003', 'TLGP-001', 'TLGP-002', or 'ALBP-001'")
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


BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Account-wide 0.0 quota block resolved 2026-09-04 (see ship_roadmap.md for
# the full saga). Legacy Claude 3 Haiku was tried first as the cheapest
# option but rejected — Anthropic marks it Legacy and denies access to
# accounts without 30 days of prior usage history, a brand-new account can
# never satisfy that. Haiku 4.5 isn't legacy-gated; needs the "us." cross-
# region inference profile prefix for on-demand invocation (confirmed
# earlier in the troubleshooting saga).

OLLAMA_MODEL_ID = "llama3.2:3b"
# Fully local, zero AWS dependency. Mechanically proven working (2026-09-04)
# but the 3B model produced a false negative on the one clear planted
# violation and appears to have skipped the mandatory RAG tool call —
# plumbing validated, judgment quality not.

GEMINI_MODEL_ID = "gemini-3.6-flash"
# Hosted (not local, so not constrained by dev-machine compute), free tier
# via Google AI Studio, independent of both the AWS account issue and
# local-model capability limits. Needs GEMINI_API_KEY set in the
# environment. Being tried specifically because Ollama's small local model
# under-performed on judgment quality/tool-use reliability.


def build_agent() -> Agent:
    backend = os.environ.get("SHIP_MODEL_BACKEND", "bedrock")
    if backend == "bedrock":
        from strands.models.bedrock import BedrockModel
        model = BedrockModel(model_id=BEDROCK_MODEL_ID)
    elif backend == "ollama":
        from strands.models.ollama import OllamaModel
        model = OllamaModel("http://localhost:11434", model_id=OLLAMA_MODEL_ID)
    elif backend == "gemini":
        from strands.models.gemini import GeminiModel
        model = GeminiModel(model_id=GEMINI_MODEL_ID)  # reads GEMINI_API_KEY from env
    else:
        raise ValueError(f"Unknown SHIP_MODEL_BACKEND: {backend!r} (expected 'bedrock', 'ollama', or 'gemini')")

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_regulation_text],
        structured_output_model=DiagnosticianOutput,
    )


def diagnose_in_process(isolated_fragment: str) -> DiagnosticianOutput:
    """Builds and runs the Strands Agent directly in this process. Faster,
    no network hop, no dependency on the deployed AgentCore runtime being
    healthy — used for the demo/dev path."""
    agent = build_agent()
    result = agent(f"Isolated code fragment flagged by Screener:\n\n{isolated_fragment}")
    return result.structured_output  # type: ignore[return-value]


AGENTCORE_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:680160265218:runtime/shipagentcore_ship_diagnostician-ziGdiLDe7Q"


def diagnose_via_agentcore(isolated_fragment: str) -> DiagnosticianOutput:
    """Calls the real deployed Bedrock AgentCore Runtime (shipagentcore/) over
    the network instead of running the agent in this process — the
    architecturally "complete" path: webhook -> deployed AgentCore runtime,
    not two parallel implementations of the same logic. Needs
    bedrock-agentcore:InvokeAgentRuntime permission (already granted)."""
    import json
    import uuid

    import boto3

    client = boto3.client("bedrock-agentcore", region_name="us-west-2")
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
        runtimeSessionId=str(uuid.uuid4()),
        payload=json.dumps({"fragment": isolated_fragment}).encode("utf-8"),
    )
    body = response["response"].read()
    # main.py's deployed invoke() returns result.structured_output.model_dump()
    # (see shipagentcore/app/ship_diagnostician/main.py) as a single JSON
    # object, confirmed empirically against the real deployed runtime
    return DiagnosticianOutput(**json.loads(body))


def diagnose(isolated_fragment: str) -> DiagnosticianOutput:
    """Entry point Triage/the webhook route call. Needs AWS credentials
    configured. Toggled via SHIP_DIAGNOSTICIAN_MODE ("in_process" | "agentcore",
    default "in_process" for demo reliability) — see diagnose_in_process vs
    diagnose_via_agentcore above for the tradeoff."""
    mode = os.environ.get("SHIP_DIAGNOSTICIAN_MODE", "in_process")
    if mode == "in_process":
        return diagnose_in_process(isolated_fragment)
    elif mode == "agentcore":
        return diagnose_via_agentcore(isolated_fragment)
    raise ValueError(f"Unknown SHIP_DIAGNOSTICIAN_MODE: {mode!r} (expected 'in_process' or 'agentcore')")


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
