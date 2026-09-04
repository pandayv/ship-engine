# SHIP

*Working name — "Smart Health Inspection for Pipelines," not yet final.*

An autonomous compliance-gatekeeper for AI-feature code changes. A CI/CD
webhook scans a pull request diff; a small pipeline of named components
detects AI-specific compliance risk (starting with PII leaking to an
external LLM), verifies the finding before trusting it, cites the actual
regulation, and drafts a remediation patch.

Built for a small fintech startup team that can't afford a dedicated
compliance hire — the founder/CTO gets the alert and closes the loop.

**Status: early scaffold, not yet functional end-to-end.** Built for the
[Agents for Humans Hackathon](https://agentsforhumans.devpost.com/)
(Strands Agents SDK), Professional Agents track.

## The pipeline

| Component | Role |
|---|---|
| **Screener** | Fast, zero-cost regex/AST pre-filter + Amazon Comprehend PII pass. Runs on every commit. |
| **Diagnostician** | Semantic evaluator (Strands Agent on Amazon Bedrock). RAG-grounded against real GDPR / EU AI Act text. Runs only on fragments Screener flags. |
| **Triage** | Routes the build: log-and-pass, or freeze pending human review. |
| **Attending** | The human-in-the-loop review console. |

(Herald and Surveillance are planned for later phases — see the project roadmap, not yet in this repo.)

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

Strands Agents SDK, Amazon Bedrock (model + Titan Embeddings), Amazon
Comprehend, Amazon DynamoDB, FastAPI, local FAISS/Chroma vector store.

## License

MIT — see `LICENSE`.
