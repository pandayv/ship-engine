# SHIP

*Working name — "Smart Health Inspection for Pipelines," not yet final.*

An autonomous compliance-gatekeeper for AI-feature code changes. A CI/CD
webhook scans a pull request diff; a small pipeline of named components
detects AI-specific compliance risk (starting with PII leaking to an
external LLM), verifies the finding before trusting it, cites the actual
regulation, and drafts a remediation patch.

Built for a small fintech startup team that can't afford a dedicated
compliance hire — the founder/CTO gets the alert and closes the loop.

**Status: core detection loop working end-to-end**, verified both in-process
and against a real deployed Bedrock AgentCore Runtime. Built for the
[Agents for Humans Hackathon](https://agentsforhumans.devpost.com/)
(Strands Agents SDK), Professional Agents track.

## The pipeline

| Component | Role |
|---|---|
| **Screener** | Fast, zero-cost regex/AST pre-filter. Runs on every commit. (Amazon Comprehend PII second-pass is coded but not yet wired in — see `src/agents/pii_confirm.py`.) |
| **Diagnostician** | Semantic evaluator (Strands Agent). RAG-grounded against real GDPR / EU AI Act text. Runs only on fragments Screener flags. Runs either in-process or against the real deployed [AgentCore Runtime](shipagentcore/) (`SHIP_DIAGNOSTICIAN_MODE=in_process\|agentcore`), and against Bedrock, Google Gemini, or a local Ollama model (`SHIP_MODEL_BACKEND=bedrock\|gemini\|ollama`) — Bedrock is the default for the real submission. |
| **Triage** | Routes the build: log-and-pass, or freeze pending human review. |
| **Attending** | The human-in-the-loop review console (`/dashboard`) — Approve/Reject a frozen alert, backed by DynamoDB. |

(Herald and Surveillance are planned for later phases — see the project roadmap, not yet in this repo.)

## AgentCore deployment

[`shipagentcore/`](shipagentcore/) is a separate CDK-managed deployment of
Diagnostician to a real Amazon Bedrock AgentCore Runtime — same system
prompt, RAG tool, and structured output as `src/agents/diagnostician.py`,
ported into the runtime's own entrypoint contract. Deployed via the official
`@aws/agentcore` CLI.

## Detectors

- **PIIE-001** (must-ship): financial PII leaking to an external LLM's prompt context.
- Additional detectors are feature-flagged and only enabled once individually verified — see the roadmap.

## Demo target

This engine is demonstrated against a fork of
[MicroPyramid/micro-finance](https://github.com/MicroPyramid/micro-finance)
(MIT licensed), used purely as a realistic third-party demo fixture — it is
**not** part of this submission's own codebase, and none of its code is
incorporated here. A single feature PR is opened against that fork to
demonstrate SHIP catching a real violation in a real pull request.

## Stack

Strands Agents SDK, Amazon Bedrock (model + Titan Embeddings) + AgentCore
Runtime, Amazon DynamoDB, FastAPI, a local numpy-based vector store (the
corpus is small enough that FAISS/Chroma would be overkill — see the
roadmap for why).

## License

MIT — see `LICENSE`.
