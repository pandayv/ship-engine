# SHIP: Project Description

*(The long-form pitch. The text to paste into Devpost is in
[`submission/`](submission/). Technical detail lives in the
[README](README.md) and the
[architecture page](https://pandayv.github.io/ship-engine/architecture.html).)*

## What it does

Every AI feature a small team ships is also a legal decision nobody signed
off on. SHIP puts a person back into that decision without slowing the
team down.

SHIP is an AI reviewer that reads a pull request the moment it opens. It
works out whether the diff crosses a line: raw personal data reaching an
external model, a protected characteristic swaying a score, an automated
decision with no human checkpoint, an AI handed more authority than it
should have. It cites the regulation it breaks, drafts the fix, and
interrupts a person only when it finds something. A confirmed violation
turns the PR's own `ship/compliance` check red, and as a required check
that stops the merge button. It is a gate, not a dashboard entry someone
has to remember to open.

Then talk to it. A voice should feel like a conversation, so SHIP asks and
you answer in plain words. *"Alexa, ask ship review what's up."* gets one
sentence. It says how many blockers there are and offers a screen to look
at, never the finding itself. Say *"the laptop"* and the detail appears
there, as one table per pull request with numbered rows. Alexa asks *"Want
to go through them?"* and takes you through the findings you have looked
at, one at a time: *"Number 1 on pull request 1. Allow, block, or skip?"*
Answer the way you would answer a colleague: *"Allow it, it's only test
data."* Leave the reason out and SHIP asks for it. Each decision shows on
the screen as pending, and at the end of a pull request one plain yes
applies the batch.

Allow means you accept the risk, and the finding stops blocking. Block
means you confirm the finding is real, and the pull request stays blocked
until a later commit changes the flagged code. SHIP checks the PR again on
every commit.

A decision by voice has to be a decision a person made. Relay, SHIP's
server, refuses unless the finding was shown on a screen in the last ten
minutes and a reason is on the record. It applies nothing until a second
step, the person's plain yes. The Alexa Skill adds two more checks. The
reason must be built from the person's own words, and only that yes can
trigger the second step. A bare yes never decides anything. In the guided
review, plain code reads every answer. No language model is involved, and
none ever sees a finding.

SHIP polices this exact pattern, an AI decision applied with no human
oversight, in other people's code. It holds itself to the same rule in its
own.

## The problem, and why now

A five-person startup adds an AI loan-underwriting feature this week. It
ships and everyone moves on. Then someone realizes the prompt included a
raw Social Security number, or a zip code was quietly swaying approvals, or
the model's opinion had become the decision with nobody checking.

GDPR Article 32 requires appropriate security for personal data. The EU AI
Act's Annex III names systems that evaluate creditworthiness as high-risk.
A small team has no compliance hire and no time to build one. Shipping
blind and slowing every PR for manual review both fail. SHIP is the third
path.

## Track & mini-challenge

**Primary track: Alexa+.** Relay, SHIP's MCP server, implements the MCP
spec (`2025-11-25`, Streamable HTTP) and runs on AWS Lambda. Alexa+ add-on
tooling is limited to select partners, and the hackathon
[FAQ](https://amazonappdev2026.devpost.com/details/faqs) says so. So an
Alexa Skill, "ship review", stands in for the add-on and is the MCP client.
Plain code runs the guided review. Amazon Nova Lite on Bedrock turns other
free speech into tool calls, and plain code checks every call. An Echo
drives the whole flow. A web simulator that
acts as an MCP client (`initialize`, `tools/list`, `tools/call`) is
included for anyone without a device.

**Mini-challenge: AWS Builder.** SHIP runs on:

- Amazon Bedrock: Nova Lite for Detector and for the skill's free speech,
  Titan Embeddings for retrieval.
- Bedrock AgentCore Runtime, which hosts Detector.
- AWS Lambda for the webhook, the fragment processor, Relay (with
  SnapStart), the screen gateway and the skill.
- Amazon SQS and a dead-letter queue between the webhook and the reviewer.
- Amazon DynamoDB, one table per access pattern.
- Amazon API Gateway WebSocket APIs for pushing findings to screens.
- AWS Systems Manager Parameter Store for the push signing key and the
  skill's credentials.
- Alexa Skills Kit, including proactive events for the Echo alert.

[Product Feedback](PRODUCT_FEEDBACK.md) covers which service did what and
how each integration went.

## Technical highlights

- **A cost-aware pipeline.** A free regex/AST pre-filter runs on every
  commit. Only flagged fragments reach the paid model call.
- **Grounded, not recalled.** Every citation traces to sourced regulation
  text retrieved at judgment time. A Healing Loop script re-checks that the
  stored text still matches the source.
- **A feedback loop.** What reviewers allow becomes a small set of general
  patterns. A pattern counts only after several independent decisions, and
  Detector treats it as context while it still judges every finding against
  the regulation text. A finding a reviewer blocked as real never feeds it.
- **Parallel, retryable review.** Each flagged fragment is its own SQS job.
  A slow or failed one can't block the others, and a dead-letter queue
  catches failures instead of dropping them.
- **A check that doesn't lie.** The PR shows `pending` the moment a commit
  arrives and `success`, `failure` or `error` afterwards, always on the
  current head commit. A review ledger keeps it from turning green before
  every fragment has reported, and an unreadable state stays pending.
- **A modality contract, enforced by the server.** A spoken sentence is
  capped at 180 characters with no line breaks, so a list of findings can't
  be read aloud. The tests check outcomes: a refusal changes nothing, an
  allowed finding stops blocking the PR's check, a blocked one keeps it
  failing.
- **Questions that take any answer.** Alexa only understands a sentence
  that matches one of a skill's sample phrases, so a free answer to a
  question falls to "I didn't understand". Every question SHIP asks uses
  slot elicitation, which makes Alexa hand back whatever the person says
  as plain words.
- **The model proposes, code decides.** In the guided review, code reads
  every answer with a wide vocabulary and no model, so the meaning of an
  answer is predictable. For other free speech the model can reach four
  tools and never sees finding text. Spoken numbers become finding ids in
  code, using the numbers the person saw on a screen. What Alexa says back
  is Relay's sentence, never the model's paraphrase.
- **Push, not polling.** A WebSocket sits idle until a finding needs to
  reach a screen. If the page is closed, a Web Push notification wakes it,
  and the findings count as seen only after the page displays them.

## Design: a coherent product

Voice, screen and decision are three surfaces doing three different jobs on
purpose. Alexa triggers, routes and asks the questions. A nearby display
renders the detail.
Deciding a finding, in Gate with a typed reason or by voice with a spoken
one, always requires having seen it first. That split is why the refusal
(allowing something you've never looked at fails) reads as a design
position and not a missing feature.

The product is also candid about what it doesn't know. If SHIP can't read
its status, it says "I can't check right now. Open the review console."
and never says all clear. When a batch of decisions only partly applies, it
says so: "Applied 2 of 3. 1 already decided in the console."

## Potential impact

The customer is a five-person startup with no compliance hire. The impact
runs in two directions: what happens to that startup, and what happens to
the people its AI feature touches.

For the startup, a single unreviewed decision can be enough to draw a
regulator's attention, and most small teams have no one whose job is to
catch it before it ships. The alternatives today are shipping blind or
slowing every PR down, and both defeat the reason they adopted AI. SHIP
costs nothing on a clean PR (a free regex/AST pass, no model call) and
interrupts a person only when something is wrong.

For that startup's customers, the applicant whose SSN reaches an external
model unmasked, the borrower whose zip code quietly moves a credit score,
the person whose loan is rejected by a model's opinion with nobody
checking, none of them ever see SHIP. They only experience the absence of
the harm it caught. The person it benefits most is never the team that
installed it.

SHIP also holds itself to its own rule. It flags an AI decision applied
with no human checkpoint in other people's code. In its own interface, no
decision applies without a person who has seen the finding, given a
reason and confirmed. The server enforces that, so it doesn't depend on the
model behaving.

## Quality of the idea

A compliance gatekeeper isn't an obvious fit for a voice assistant, and
that's the point. Most voice submissions extend something that already has
a screen. SHIP inverts it. The finding lives on a screen by design, and
voice's job is saying whether one exists, where to look, and then asking
about each finding and taking the decision once the person has looked.

The sharper move is to hold SHIP's own interface to the standard it
enforces on others. The failure SHIP exists to catch is not "a decision
happened over voice". It is a decision with no review behind it. So voice
can confirm a decision after a person has seen the evidence and said why,
and it can't make one blind.

**Try it:** on an Echo, say "Alexa, ask ship review what's up" and answer what
it asks. Without a device, use the
[web simulator](https://pandayv.github.io/ship-engine/alexa-simulator.html)
and the [screen display page](https://pandayv.github.io/ship-engine/ship-display.html).
The [architecture page](https://pandayv.github.io/ship-engine/architecture.html)
walks through every component.
