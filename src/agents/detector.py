"""
Detector, SHIP's semantic evaluator. Runs only on fragments Screener has already flagged. A
Strands Agent, RAG-grounded against real regulation text (never unaided model recollection).

The model backend is switchable via SHIP_MODEL_BACKEND ("bedrock" | "ollama" | "gemini", default
"bedrock"). Detector itself is switchable via SHIP_DETECTOR_MODE ("in_process" default |
"agentcore", see detect() below). "agentcore" calls the deployed Bedrock AgentCore Runtime over
the network instead of building the Strands Agent in this process, which is why the strands and
numpy imports in this file are lazy.

Scope: the prompt covers PIIE-001/002/003 (GDPR Art. 32), TLGP-002 (EU AI Act Art. 14), ALBP-001
(EU AI Act Art. 10 + Annex III Section 5(b)), and TLGP-001 (OWASP LLM06:2025 Excessive Agency, an
industry security standard rather than a law, and Detector says so in its output). DPSL-002/003
and TLGP-003 are out of scope because a single code diff cannot show them: they need
infrastructure or deployment configuration, or observation across files or over time. A category
joins Detector only when it is both detectable in a diff and has a sourced grounding corpus.
Asking Detector to judge anything else risks the hallucinated citations that "Trust but Verify"
exists to prevent.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from strands import Agent

# strands/numpy (via src.rag.vector_store) are DELIBERATELY NOT imported at
# module level — only detect_in_process()'s path needs them. This lets a
# caller that only ever uses detect_via_agentcore() (e.g. the public
# webhook deployment, which delegates the heavy lifting to the already-
# deployed AgentCore runtime over the network) import this module without
# pulling in strands-agents/numpy's dependency tree at all — the difference
# between a plain zip-based Lambda deployment and needing a container.

SYSTEM_PROMPT = """You are the SHIP Detector, a compliance auditor for a small \
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

Also call `retrieve_prior_determinations` to check whether this looks like a \
shape human reviewers have confirmed before as a false positive. Treat what it \
returns as CONTEXT, never a rule — it does not override your own grounded \
judgment against the actual regulation text, and a pattern existing does not by \
itself mean this specific case is safe. Weigh it, do not defer to it.

If you confirm a real violation:
- Set taxonomy_id to whichever ID actually applies (PIIE-001/002/003, TLGP-001, \
  TLGP-002, or ALBP-001).
- Assign a risk_score from 1-10. Each category is judged and frozen against \
  its own bar (these numbers match what \
  Triage enforces per category, see src/taxonomy.py's REGISTRY): \
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


class DetectorOutput(BaseModel):
    # Strands surfaces this description to the model as part of the structured-output schema,
    # so it must match the prompt's scope.
    matched: bool = Field(description="True only if a real violation was confirmed (any of PIIE-001/002/003, TLGP-001, TLGP-002, ALBP-001), not just Screener's keyword match")
    taxonomy_id: str | None = Field(default=None, description="'PIIE-001', 'PIIE-002', 'PIIE-003', 'TLGP-001', 'TLGP-002', or 'ALBP-001'")
    risk_score: float | None = Field(default=None, ge=1, le=10)
    plain_english_summary: str | None = None
    citation: str | None = Field(default=None, description="Exact article/paragraph from the retrieved regulation text")
    remediation_patch: str | None = None

    @model_validator(mode="after")
    def _matched_implies_populated(self) -> "DetectorOutput":
        # "matched implies all four fields populated" is enforced here, once, so a prompt
        # regression fails loudly at the model boundary instead of silently becoming an empty
        # string downstream, or crashing triage.route() on matched=True with no risk_score.
        if self.matched:
            missing = [
                name for name, value in (
                    ("taxonomy_id", self.taxonomy_id),
                    ("risk_score", self.risk_score),
                    ("citation", self.citation),
                    ("plain_english_summary", self.plain_english_summary),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"matched=True but missing required field(s): {', '.join(missing)}")
        return self


@lru_cache(maxsize=1)
def _get_store():
    # lru_cache does not cache a raised exception, so a transient embedding failure is retried
    # on the next call instead of poisoning the cache for the rest of the process's lifetime.
    from src.rag.chunker import chunk_corpus
    from src.rag.embedding_cache import default_cache_path
    from src.rag.vector_store import VectorStore

    corpus_dir = Path(__file__).resolve().parents[2] / "rag_corpus"
    chunks = chunk_corpus(corpus_dir)
    store = VectorStore()
    # Uses the precomputed embeddings when they match this corpus and
    # model; falls back to live Bedrock Titan calls otherwise (needs AWS
    # credentials either way — queries are always embedded live).
    store.build(chunks, cache_path=default_cache_path(corpus_dir))
    return store


# Unlike the regulation corpus (genuinely static between deploys, hence
# _get_store()'s permanent lru_cache), learned patterns change within one
# deployment's lifetime as scripts/pattern_miner.py runs periodically. A
# permanent cache would mean a pattern that just crossed MIN_SUPPORT never
# gets used by an already-warm container until it happens to cold-start —
# silently, with no error to notice. But rebuilding (and re-embedding via
# Bedrock) on every single fragment review is the same real, measured cost
# problem src/rag/embedding_cache.py's docstring documents for the
# regulation corpus. This is the proportionate middle: cached, but with a
# bound on staleness, not cached forever and not rebuilt every call.
_PATTERN_STORE_REFRESH_SECONDS = 1800  # 30 minutes
_pattern_store_cache: tuple = (None, 0.0)  # (PatternStore | None, cached_at monotonic)


def _get_pattern_store():
    import time

    from src.rag.pattern_store import PatternStore, list_patterns

    global _pattern_store_cache
    store, cached_at = _pattern_store_cache
    if store is not None and (time.monotonic() - cached_at) < _PATTERN_STORE_REFRESH_SECONDS:
        return store

    store = PatternStore()
    store.build(list_patterns(eligible_only=True))
    _pattern_store_cache = (store, time.monotonic())
    return store


DEFAULT_BEDROCK_MODEL_ID = "amazon.nova-lite-v1:0"
# Chosen on measured evidence (scripts/benchmark_models.py, run against the planted
# violations in micro-finance PRs #1 and #2):
#
#   model              violations caught   look-alikes dismissed   median   req/min
#   nova-lite                        6/6                     4/4     3.1s       200
#   nova-pro                         6/6                     4/4     4.5s        25
#   qwen3-235b                       6/6                     4/4     4.5s       100
#   glm-5                            6/6                     4/4     7.4s       100
#   claude-haiku-4-5                 6/6                     4/4    13.4s        10
#   deepseek-v3.2                    6/6                     4/4    18.7s       100
#
# Identical accuracy, four times faster, twenty times the request quota —
# and requests per minute, not tokens, is what bounds how many pull
# requests SHIP can review (tokens/min sits at 5,000,000 and is never the
# limit). Being first-party Amazon also keeps the whole path AWS-native.
# No "us." inference-profile prefix needed, unlike the Anthropic models.
#
# The evidence is one pass per case on fixtures built to be unambiguous. It shows nova-lite is
# not worse on these ten cases.

# Overridable so a model can be swapped without editing code. The
# per-model Bedrock request-per-minute quota varies by a factor of twenty
# on this account (Claude models are capped at 10/min, Amazon Nova Lite at
# 200/min), and that ceiling — not token throughput — is what bounds how
# many pull requests SHIP can review per minute. Being able to A/B a model
# against the adversarial fixture set without a code change is what makes
# that a measurable decision instead of a guess.
BEDROCK_MODEL_ID = os.environ.get("SHIP_BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL_ID)
# Claude 3 Haiku is legacy-gated (Anthropic denies it to accounts without 30 days of prior
# usage), so a new account cannot use it. Haiku 4.5 is not gated but needs the "us."
# cross-region inference profile prefix for on-demand invocation.

OLLAMA_MODEL_ID = "llama3.2:3b"
# Fully local, zero AWS dependency. The 3B model missed the planted violation and skipped the
# mandatory RAG tool call in testing, so use it to exercise the pipeline, not for real reviews.

GEMINI_MODEL_ID = "gemini-3.6-flash"
# Hosted (not constrained by dev-machine compute), free tier via Google AI Studio. Needs
# GEMINI_API_KEY set in the environment.


def build_agent() -> "Agent":
    from strands import Agent, tool

    @tool
    def retrieve_regulation_text(query: str) -> str:
        """Retrieve the most relevant real regulation text chunks for a query
        about a possible compliance violation. Always call this before citing
        any law."""
        results = _get_store().query(query, top_k=3)
        return "\n\n".join(f"[{r.chunk.article}, para {r.chunk.paragraph_index}] {r.chunk.text}" for r in results)

    @tool
    def retrieve_prior_determinations(query: str) -> str:
        """Retrieve patterns learned from past human dismissals of similar
        findings — cases a human reviewer confirmed were NOT actually
        violations, generalized from specific instances. Call this after
        Screener flags something, to check whether this looks like a known
        false-positive shape. These are CONTEXT to weigh, never a rule to
        follow blindly — still form an independent judgment grounded in the
        actual regulation text; a pattern can inform that judgment but
        cannot substitute for it."""
        results = _get_pattern_store().query(query, top_k=3)
        if not results:
            return "No relevant prior determinations found."
        return "\n\n".join(f"[confirmed {p.support_count}x] {p.text}" for p in results)

    backend = os.environ.get("SHIP_MODEL_BACKEND", "bedrock")
    if backend == "bedrock":
        from strands.models.bedrock import BedrockModel

        from src.aws.bedrock_session import BEDROCK_RETRY_CONFIG, bedrock_session

        # Shares the process-wide boto3 Session with vector_store.py's Bedrock embedding calls
        # and uses the same adaptive-retry Config, so when concurrent fragment processing trips
        # the account quota, this call backs off and retries automatically.
        model = BedrockModel(
            model_id=BEDROCK_MODEL_ID, boto_session=bedrock_session(), boto_client_config=BEDROCK_RETRY_CONFIG,
        )
    elif backend == "ollama":
        from strands.models.ollama import OllamaModel
        # Reads OLLAMA_HOST, the same source vector_store.py's _embed_ollama() uses.
        ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        model = OllamaModel(ollama_host, model_id=OLLAMA_MODEL_ID)
    elif backend == "gemini":
        from strands.models.gemini import GeminiModel
        model = GeminiModel(model_id=GEMINI_MODEL_ID)  # reads GEMINI_API_KEY from env
    else:
        raise ValueError(f"Unknown SHIP_MODEL_BACKEND: {backend!r} (expected 'bedrock', 'ollama', or 'gemini')")

    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[retrieve_regulation_text, retrieve_prior_determinations],
        structured_output_model=DetectorOutput,
    )


def detect_in_process(isolated_fragment: str) -> DetectorOutput:
    """Builds and runs the Strands Agent directly in this process. Faster,
    no network hop, no dependency on the deployed AgentCore runtime being
    healthy — used for the demo/dev path."""
    agent = build_agent()
    result = agent(f"Isolated code fragment flagged by Screener:\n\n{isolated_fragment}")
    # structured_output is Optional in Strands' AgentResult (a run that ends via interrupt,
    # for example), so None is an error here.
    if result.structured_output is None:
        raise RuntimeError("Agent run completed without producing structured output — no verdict to return.")
    return result.structured_output


def _agentcore_runtime_arn() -> str:
    # Required env var with no fallback default. Redeploys can mint a new ARN, so a missing or
    # wrong value fails loudly the first time detect_via_agentcore() runs instead of silently
    # invoking a stale runtime.
    arn = os.environ.get("SHIP_AGENTCORE_RUNTIME_ARN")
    if not arn:
        raise RuntimeError(
            "SHIP_AGENTCORE_RUNTIME_ARN is not set — detect_via_agentcore() has no runtime to "
            "call. Set it to the current runtime ARN (visible in `agentcore status` or the "
            "deploy output) after every `agentcore deploy`, since redeploys can mint a new ARN."
        )
    return arn


def detect_via_agentcore(isolated_fragment: str) -> DetectorOutput:
    """Calls the deployed Bedrock AgentCore Runtime (shipagentcore/) over the network instead of
    running the agent in this process, so the webhook and the runtime share one implementation
    of the logic. Needs bedrock-agentcore:InvokeAgentRuntime permission.

    A single invoke_agent_runtime call can take several minutes when the runtime is pacing a
    cold-start corpus-embedding burst plus the model's own calls (190s to 308s in real runs),
    and botocore's default read timeout is 60s. So this sets an explicit read_timeout with
    margin under the Lambda's function timeout, and disables botocore's retry-on-timeout
    (max_attempts=1): a retry would double the wait past the Lambda's budget instead of
    failing fast and letting the per-fragment handler record one clear error.
    """
    import json
    import uuid

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-agentcore",
        region_name="us-west-2",
        config=Config(read_timeout=540, connect_timeout=10, retries={"max_attempts": 1}),
    )
    response = client.invoke_agent_runtime(
        agentRuntimeArn=_agentcore_runtime_arn(),
        runtimeSessionId=str(uuid.uuid4()),
        payload=json.dumps({"fragment": isolated_fragment}).encode("utf-8"),
    )
    body = response["response"].read()
    # main.py's deployed invoke() returns result.structured_output.model_dump()
    # (see shipagentcore/app/ship_diagnostician/main.py) as a single JSON object
    return DetectorOutput(**json.loads(body))


def detect(isolated_fragment: str) -> DetectorOutput:
    """Entry point Triage/the webhook route call. Needs AWS credentials
    configured. Toggled via SHIP_DETECTOR_MODE ("in_process" | "agentcore",
    default "in_process" for demo reliability) — see detect_in_process vs
    detect_via_agentcore above for the tradeoff."""
    mode = os.environ.get("SHIP_DETECTOR_MODE", "in_process")
    if mode == "in_process":
        return detect_in_process(isolated_fragment)
    elif mode == "agentcore":
        return detect_via_agentcore(isolated_fragment)
    raise ValueError(f"Unknown SHIP_DETECTOR_MODE: {mode!r} (expected 'in_process' or 'agentcore')")


if __name__ == "__main__":
    # Will only run once AWS credentials are configured (Bedrock + the
    # embedding calls inside retrieve_regulation_text both need them).
    #
    # This fixture matches screener.py's __main__ fixture. They are separate, self-contained
    # smoke tests for running each module standalone, so the copy is kept in sync by hand.
    from src.agents.screener import isolate_fragment, scan

    bad_sample = '''
import openai

def get_underwriting_opinion(client):
    prompt = f"Applicant {client.first_name} {client.last_name}, DOB {client.date_of_birth}, " \\
             f"income {client.annual_income}, blood group {client.blood_group}. Should we approve?"
    response = openai.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
    return response
'''
    screener_result = scan(bad_sample)
    fragment = isolate_fragment(bad_sample, screener_result.matched_terms)
    print("Screener isolated fragment, handing to Detector:\n", fragment)
    verdict = detect(fragment)
    print(verdict.model_dump_json(indent=2))
