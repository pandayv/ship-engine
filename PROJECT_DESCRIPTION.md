# SHIP — Project Description

*(Devpost submission text. Full technical detail lives in the repo's own
[README](README.md); this is the pitch.)*

## What it does

Every AI feature a small team ships is also a legal decision nobody
signed off on. SHIP is an AI reviewer that reads a pull request the
moment it opens. It works out whether the diff actually crosses a real
line: raw personal data reaching an external model, a protected
characteristic swaying a score, an automated decision with no human
checkpoint. Then it cites the exact regulation broken, drafts the fix,
and only interrupts a human when it's found something real. Confirm a
violation and the PR's own merge button gets blocked, for real, via a
GitHub commit status. It's a working gate, not a dashboard entry someone
has to remember to check.

Then ask it out loud. *"Alexa, what's up?"* gets one honest sentence: how
many findings, how serious, which pull request. Never the finding itself.
Say "show me on the TV" and the actual detail appears, live, on whichever
screen answered to that name: a TV, an iPad, a laptop, a smart fridge's
browser. Ask it to approve a blocking finding by voice and it refuses on
purpose, enforced in the server rather than by asking a model to behave.
There is no tool in its interface that can resolve a compliance decision
without a person leaving a written reason on the record. SHIP polices
this exact pattern, an AI decision applied with no human oversight, in
other people's code. It holds itself to the same rule in its own.

## The problem, and why now

A five-person startup adds an AI loan-underwriting feature this week. It
ships, everyone moves on. Then someone realizes the prompt included a raw
Social Security number, or a zip code was quietly swaying approvals, or
the model's opinion had become the actual decision with nobody checking.
GDPR's security obligations (Article 32) have been binding, actively
enforced law since 2018, with real fines reaching tens of millions of
euros for exactly this. Credit-scoring AI is separately named in the EU
AI Act's Annex III as a high-risk use case, on a compliance timeline
that's real even though it moved once already. That deadline was delayed
from August 2026 to December 2027 by a mid-2026 amendment, checked
directly against current EU sources for this submission. A small team
shipping fast has no compliance hire and no time to build one. Shipping
blind and slowing everything down for manual review both fail. SHIP is
the third path.

## Track & mini-challenge

**Primary track: Alexa+.** Relay, SHIP's MCP server, implements the MCP
spec (`2025-11-25`, Streamable HTTP) and is deployed live on AWS Lambda.
The submission uses the hackathon's own explicitly-sanctioned
simulated-Alexa+-experience path (a real web app, source included,
calling the identical deployed Relay endpoint) rather than the
CLI/device path, because that path's account-registration requirement
isn't self-service (see Product Feedback and `FRICTION_LOG.md` for
exactly where and how that surfaced). The simulation isn't a mock. Every
voice response and every pushed finding in the demo comes from the real,
running system.

**Mini-challenge: AWS Builder.** Amazon Bedrock (Nova Lite, chosen on a
measured six-model benchmark rather than preference), Bedrock AgentCore
Runtime, four purpose-scoped Lambda functions, SQS, four DynamoDB tables,
and an API Gateway WebSocket API for real device push. See
[Product Feedback](PRODUCT_FEEDBACK.md) for which service did what and
how each integration actually went.

## Technical highlights

- **A cost-aware pipeline.** A free regex/AST pre-filter runs on every
  commit. Only flagged fragments ever reach the paid model call.
- **Grounded, not recalled.** Every citation traces to real, sourced
  regulation text retrieved at judgment time, verified with a live
  Healing Loop that re-checks the source pages haven't since been amended
  out from under a stored citation.
- **Parallel, retryable review.** Each flagged fragment in a PR is its own
  SQS-queued job. A slow or failed one can't block review of the others,
  and a dead-letter queue catches genuine failures instead of silently
  dropping them.
- **A modality contract, enforced server-side.** Voice responses are
  hard-capped to one sentence with no line breaks, so a finding list
  physically cannot be spoken. Verified with tests that assert the
  absence of a capability, no tool can approve a risk, not only the
  presence of features.
- **Push, not polling.** A WebSocket connection sits idle until a finding
  actually needs to reach a device, matched to how rarely a real
  compliance event happens rather than to a fixed refresh interval.

## Design: a coherent product

Voice, screen, and decision are three different surfaces doing three
different jobs on purpose. Alexa triggers and routes. Any nearby display
renders the detail. Gate, with a mandatory typed, attributed reason on
every decision, is the only place anything gets resolved. That split is
why the refusal (asking to approve by voice fails, on purpose) reads as a
real design position rather than a missing feature.

## Potential impact and idea quality

Every property claimed above is checked against the real, deployed
system: a live Lambda, live DynamoDB, a live WebSocket push, a real
GitHub PR whose merge button actually gets blocked. None of it is
asserted from a passing test suite alone. The persona, a small team with
no compliance hire, is specific, and the mechanism (deterministic
pre-filter, grounded semantic judgment, human-gated resolution) is the
same "verify before trusting" pattern that stays durable well past this
hackathon.

## Pre-existing project disclosure

The core review engine (Screener → Detector → Triage → Gate) began as a
submission to the separate *Agents for Humans Hackathon* (Strands SDK).
Everything described in "Ask, don't read" in the README, Relay, the
modality contract, the Alexa+ simulation, and real cross-device push, was
built new, from scratch, inside this hackathon's own submission window
(opened August 31, 2026). `git log` shows this directly, commit by
commit.
