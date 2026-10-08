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
spec (`2025-11-25`, Streamable HTTP) and runs live on AWS Lambda. Alexa+
add-on tooling is limited to select partners, so an Alexa Skill stands in
for the add-on. It turns what Alexa hears into calls to Relay, and an Echo
drives the whole flow. A web simulator that acts as an MCP client is
included for anyone without a device. Every voice response and pushed
finding comes from the running system.

**Mini-challenge: AWS Builder.** SHIP runs on Amazon Bedrock (Nova Lite,
picked after benchmarking six models), Bedrock AgentCore Runtime, Lambda,
SQS, DynamoDB, and an API Gateway WebSocket API that pushes findings to
screens. See
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
renders the detail. Resolving a finding, whether through Gate's typed
reason or a spoken one, always requires having actually seen it first.
That split is why the refusal (approving something you've never looked
at fails, on purpose) reads as a real design position rather than a
missing feature.

## Potential impact

The customer is a five-person startup with no compliance hire. The
impact runs in two directions: what happens to that startup, and what
happens to the people its AI feature touches.

For the startup, a single unreviewed decision is already enough to
trigger a real GDPR fine or an EU AI Act investigation, and most small
teams have no one whose job is to catch it before it ships. The honest
alternatives today are shipping blind or slowing every PR down for
manual review, and both defeat the reason they adopted AI in the first
place. SHIP costs nothing on a clean PR (a free regex/AST pass, no model
call) and only interrupts a human when something real is wrong, so it
doesn't force that tradeoff.

For that startup's customers, the applicant whose SSN reaches an
external LLM unmasked, the borrower whose zip code quietly moves a
credit score, the person whose loan gets rejected by a model's opinion
with nobody checking, none of them ever see SHIP. They only experience
the absence of the harm it caught. That's the actual measure of the
product: the person it benefits most is never the team that installed
it.

Three things make that more than a demo-day claim:
- **It gets sharper with use, not just faster.** Every dismissal teaches
  Detector what a false alarm looks like, confirmed independently before
  it can shape a future judgment, so accuracy compounds instead of one
  bad call quietly eroding it.
- **It holds itself to its own rule.** SHIP flags an AI decision applied
  with no human checkpoint in other people's code. In its own interface,
  no decision resolves without a person who has actually engaged with
  the specific evidence, a constraint the server enforces, not a
  courtesy the model is asked to honor.
- **Every claim above is checked against the real, deployed system**, a
  live Lambda, live DynamoDB, a live WebSocket push, a real GitHub PR
  whose merge button gets blocked, connected through a real webhook to a
  real external repo. None of it is asserted from a passing test suite
  alone.

## Quality of the idea

A compliance gatekeeper isn't an obvious fit for a voice assistant, and
that's the point. Most voice submissions extend something that already
has a screen. SHIP inverts it: the finding lives on a screen by design,
and voice's only job is saying whether one exists and where to look,
enforced by a hard sentence cap rather than a prompt asking politely.

The sharper move is one step further. SHIP polices "an AI decision
applied with no human checkpoint" in other people's code, and holds its
own interface to the identical standard as a server-side constraint, not
a stated principle. A voice-only approval works, but only once a person
has genuinely engaged with the actual evidence. That rule came from
directly correcting an earlier, blunter version mid-build, not from a
first pass: the original design simply refused every voice approval,
until it became clear the real failure mode was never voice itself, it
was a decision with no review behind it at all.
