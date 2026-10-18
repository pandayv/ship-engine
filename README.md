# SHIP

Fast-moving teams shipping Gen AI features routinely introduce compliance hazards without meaning to: unmasked personal data (an SSN, a zip code) sitting in a prompt, race or gender quietly swaying a credit decision, an AI decision loop running with no one watching it.

These hazards hurt the people affected, and risk penalties under GDPR and the EU AI Act.

A small team can't afford a dedicated compliance hire, and reviewing every PR by hand would grind feature velocity to a crawl.

SHIP closes that gap: it catches AI compliance risk before it ships, without slowing the team down.

**SHIP is a multi-agentic gatekeeper that reads every pull request the moment it opens, catches the ones that quietly cross a legal line, and interacts with you only when it's found something.**

Ask it by voice anytime. It answers in one sentence and shows the full detail on the screen you choose.

Built for [Build, Ship, Shape: the Amazon Developer Hackathon](https://amazonappdev2026.devpost.com/) (Alexa+ track, AWS Builder mini-challenge).

![Gate, SHIP's review console, showing a blocking finding with its GDPR and EU AI Act citations, risk score, and suggested fix](docs/screenshots/gate-dashboard.png)

---

## Guiding principles

**A person decides.**
SHIP finds, explains and proposes. Only a person who has looked at the evidence decides, and the reason goes on the record.

**Last line of defense.**
Assume nothing was checked before this. It doesn't matter who wrote the code, a person or an AI.

**Grounded, not guessed.**
Rely on facts, not memory or guesswork. Every citation traces to sourced regulation text.

**Meet you where you are.**
The answer finds you, on whatever device is nearest. Voice gives the headline. A screen gives the detail.

**Learns from your decisions.**
What reviewers allow shapes the next review. Detector still judges every finding against the regulation text.

**Honest when unsure.**
A status SHIP can't read is never called all clear. A review that hasn't finished never shows a green check.

**Scalability and resilience.**
Survive retries, failures and heavy load without breaking.

**Cost efficiency.**
Free deterministic checks first, a model only when needed.

## The pipeline

SHIP is five pieces working as one pipeline, each named for its job:
Screener, Detector, Triage, Gate, Relay.

1. **A PR opens, and Screener checks it first.** A free regex/AST scan
   runs on every commit. If nothing matches, the PR passes in
   milliseconds and no model call is spent.
2. **If something matches, Detector takes a closer look.** It reviews
   just that fragment, grounded against sourced regulation text, and
   returns a verdict: risk score, plain-English explanation, citation,
   and a draft fix.
3. **Triage routes the verdict.** A finding above the line blocks the
   merge: the PR's own commit status turns red, and as a required check
   in branch protection that stops the merge button. A finding below the
   line is flagged for a human without blocking.
4. **A person decides in Gate**, the review console, or by voice. They
   see file, risk, explanation, citation and suggested fix, then
   allow or block.
5. **Relay carries the same review to voice and screen**, so it comes
   to you wherever you are.

A person makes one of two calls. **Allow** means you accept the risk. The
finding stops blocking, and the pull request can merge once nothing else
blocks it. **Block** means the finding is real. The pull request stays
blocked until a later commit changes the flagged code. Either way, your
decision and your reason are posted to the PR. In Gate, a blocking finding
offers "Allow merge" and "Keep blocked".

SHIP watches for four kinds of risk: exposing sensitive personal data,
decisions made with no human review, bias from a protected trait, and
AI handed more authority than it should have. The detectors are grounded
in GDPR, the EU AI Act and OWASP, and are listed in
[architecture.html](https://pandayv.github.io/ship-engine/architecture.html).

## What it feels like

### By voice

When SHIP records a new finding, your Echo gets a notification from SHIP
("You have 2 new unread messages from SHIP"). Nothing is read out until
you ask.

> *"Alexa, ask ship review what's up."*
> **"2 blockers found. Ready for your decision. Want it on the laptop or the tablet?"**
> *"Show it on the laptop."*
> **"Putting it on your laptop. Say allow or block, with the number and a reason."**

The findings appear on the laptop. SHIP offers only the screens that
exist. With one screen it asks "Want it on the laptop?". With none it
says so and puts a link to the review console in your Alexa app.

You decide in your own words, using the numbers on the screen:

> *"Allow the first one on pull request 2 because it's only test data."*
> **"Got it, take a look and say proceed when you're ready."**
> *"Yes."*
> **"Done."**

The decision is staged first and shown on the screen as pending. Nothing
is applied until you confirm. Say no, cancel or stop, and it is dropped.
When some decisions can't be applied, SHIP says so plainly, for example
"Applied 2 of 3. 1 already decided in the console."

A blocked finding keeps holding the merge, and voice says so: "Nothing
needs a decision, but 1 finding that was blocked still holds the merge
until the code is fixed."

### On a screen

The findings screen shows one table per pull request, blockers first.
Each row has a number, the way you speak it ("first one on pull request
2"), the file, what's wrong and the rule it breaks. A finding that
blocks the merge is marked "Needs a decision". One that doesn't is marked
"Flagged". Staged decisions show on their row as pending, for example
"Pending: allow" with your reason. When blocked findings still hold a
pull request, a note says so.

### Voice can confirm a decision. It can never make one blind.

Asking SHIP to allow a finding you haven't looked at gets a refusal.
That is the pattern SHIP flags in *other* people's code, an AI-mediated
decision applied with no human checkpoint. A decision goes through only
when three things are true.

- You looked at the finding on a screen.
- You gave a reason in your own words.
- You confirmed what SHIP understood.

The model that understands your speech never sees a finding. It turns
what you said into a proposal, and plain code checks every part of it.
Relay, the server, also refuses a decision on a finding you haven't seen
or one without a reason, so the model can't talk its way past the rules.

## Why not Alexa+

SHIP is designed as an Alexa+ experience, but Alexa+ add-ons aren't open
to hackathon participants. The hackathon
[FAQ](https://amazonappdev2026.devpost.com/details/faqs) says the add-on
tools are for select partners only, and the documented route to them
stops at an AWS role that only accounts registered by an Amazon Alexa
Solutions Architect can assume (see the [friction log](FRICTION_LOG.md)).
So a custom Alexa Skill called "ship review" stands in for the add-on.
It does the same job. It hears you and calls the same MCP server an
add-on would. A Bedrock model, Amazon Nova Lite, turns free speech into
tool calls. When the add-on opens, it can point at the same server and
the skill steps aside.

## Set up a screen

A screen is any browser: a laptop, a tablet, a TV, a smart fridge.

1. Open the [display page](https://pandayv.github.io/ship-engine/ship-display.html)
   on the device and pick a name (TV, Tablet, Laptop, Fridge, or your own).
2. Press "Get alerts on this screen" and allow notifications once.

That's all. There is no pairing code. After that, SHIP can reach the
screen even when the page is closed. It sends a notification, you tap
it, and the findings open on the page. On an iPhone or iPad, add the page
to the Home Screen first.

A notification doesn't count as a look. Findings count as seen only when
a page displays them and reports back, and only while the page is
visible. A screen's held findings expire after ten minutes.

To get the Echo alert, say "Alexa, ask ship review to turn on alerts".
Alexa sends a permission request to your Alexa app. Allow it there.

## What you can say

Open the skill with "Alexa, open ship review", or put a request after
"Alexa, ask ship review". Say it the way you'd say it to a colleague.
A few examples:

| You want to | You can say |
|---|---|
| Know where things stand | "What's up?" "Can we ship today?" "Any blockers?" "Is anything blocked?" |
| See the detail | "Show it on the laptop." "Put it on my tablet." "Send it to the TV." |
| Decide | "Allow the first one on pull request 2 because it's only test data." "Block the second one because it leaks a phone number." "Allow the first two on pull request 1 because they are test fixtures." |
| Confirm or back out | "Yes." "No." "Cancel." |
| Get alerted | "Turn on alerts." |

You can also say approve, accept, dismiss, clear or ignore instead of
allow, and reject or confirm instead of block. SHIP understands them and
answers in allow and block.

SHIP asks when something is missing, such as "What's the reason?" or
"Which screen?", and says no when a rule says no.

## Try it

- **[The simulator](https://pandayv.github.io/ship-engine/alexa-simulator.html)**
  is a web page that acts as an MCP client of Relay. It opens a session,
  lists the tools, asks for the status and shows the findings on a
  screen. Decisions are made by voice on the Echo or in the review
  console, not on this page.
- **[The display page](https://pandayv.github.io/ship-engine/ship-display.html)**
  registers a screen. Open it in one tab, then pick that screen in the
  simulator and, when something is waiting, the findings arrive there.
- **[The architecture page](https://pandayv.github.io/ship-engine/architecture.html)**
  walks through every component.

## How it's built

### Architecture

Full diagram and component-by-component detail: [pandayv.github.io/ship-engine](https://pandayv.github.io/ship-engine/architecture.html) ([source](docs/architecture.html)). How a spoken decision is checked is in [docs/voice-resolution-workflow.md](docs/voice-resolution-workflow.md).

**Each flagged issue in a PR is reviewed as its own independent,
retryable job**, queued and picked up by an independently scaling
reviewer function, rather than one sequential pass through the whole PR.
A fragment that fails outright retries automatically and lands in a
dead-letter queue after repeated failure. A fragment that is slow, or
that hits the model provider's request-rate limit, backs off and retries
without blocking anything else. A PR with several flagged issues takes
about as long as its slowest single issue, not the sum of all of them.

**The pull request check follows the whole lifecycle.** The webhook
posts `ship/compliance` as `pending` the moment it accepts a commit.
The result is `success`, `failure` or `error` afterwards, always posted
to the PR's current head commit. A review ledger records which
fragments the webhook queued, and a green result waits until every one
has reported. If SHIP can't read its state, the check stays pending.

| State of the findings | Check |
|---|---|
| A blocking finding waits for a decision | failure, "1 finding needs a decision" |
| A blocked finding's code is still in the PR | failure, "1 finding blocked by a reviewer, fix the code" |
| Only flagged (non-blocking) findings wait | success, "flagged for review" |
| Allowed, or nothing found | success |
| Review couldn't run (unreadable diff, repo check failed, fragment too large to queue) | error |

### Tech stack

- **Agent framework:** [Strands Agents SDK](https://github.com/strands-agents/sdk-python)
- **Models:** Amazon Nova Lite as the primary Bedrock backend, chosen on
  measured evidence. Benchmarked against Claude Haiku 4.5, Nova Pro,
  Qwen3-235B, GLM-5 and DeepSeek-V3.2 on the planted violations and
  look-alikes in the demo repository's PRs: all caught every violation
  and dismissed every look-alike, so per-model request-per-minute quota
  decided it (10/min for Claude models, 200/min for Nova Lite on this
  account). See the benchmark table in
  [`src/agents/detector.py`](src/agents/detector.py). Google Gemini
  serves as a credit-exhaustion fallback and a local Ollama model as an
  offline fallback, both switchable with one environment variable.
- **Agent runtime:** Amazon Bedrock AgentCore Runtime hosts the same
  Detector logic ([`shipagentcore/`](shipagentcore/)). It is callable
  in-process for local development or remotely for the deployed path.
- **Retrieval:** Amazon Bedrock Titan Embeddings and a small local numpy
  cosine-similarity store. The sourced regulation corpus is small, so a
  hosted vector database would be overhead.
- **Compute:** AWS Lambda functions for the webhook (verify, screen,
  dispatch), the fragment processor (the model call, one fragment per
  invocation), Relay, the WebSocket device gateway and the Alexa Skill.
- **Queueing:** Amazon SQS with a dead-letter queue, and a concurrency
  cap on the processor so parallel reviews stay within the account's
  request-rate limit.
- **State:** Amazon DynamoDB, one table per access pattern.
  `ship-alerts` holds findings (every write is idempotent, and a retry
  can't reopen a decision). `ship-repos` holds watched repositories and
  when each last sent an event and last finished a review.
  `ship-status` holds the precomputed release summary, so Relay's voice
  path is one `GetItem`, and the per-pull-request review ledger.
  `ship-recent-reviews` holds which finding was shown on a screen, when,
  and under what number. `ship-pending-dispositions` holds decisions
  staged by voice. `ship-device-connections` holds open screens.
  `ship-push-subscriptions` and `ship-pending-display` hold the browsers
  registered for alerts and the findings held for them.
  `ship-alexa-users` holds who SHIP may notify. `ship-learned-patterns`
  holds patterns for Detector.
- **Web:** FastAPI (the webhook route and the Gate console in one
  deployable app, wrapped for Lambda with Mangum).
- **Voice/MCP:** [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  (Streamable HTTP, spec `2025-11-25`) powers Relay
  ([`src/relay/`](src/relay/)), deployed as its own Lambda with
  [SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html).
  A fresh ASGI app is built per invocation because the SDK's session
  manager can run its lifespan only once per instance. The docstring at
  the top of [`src/relay/server.py`](src/relay/server.py) has the details.
- **Alexa Skill:** [`alexa-skill/`](alexa-skill/) stands in for an Alexa+
  add-on. Common requests have their own intents and are answered by
  plain code. Anything else goes to Amazon Nova Lite on Bedrock. Decisions
  arrive on `AllowIntent` and `BlockIntent`, one per verb, so the verb
  survives Alexa's carrier-phrase handling. Approve, accept, clear,
  dismiss and ignore reach the model as allow, and reject and confirm as
  block. The model chooses among `release_status`, `display_on`, `stage_decisions` and
  `proceed`. Plain code then checks the choice. The model never sees
  finding text. It must name a screen the person used, give numbers the
  person saw, and give a reason built from the person's own words. Only
  the person's plain yes can apply a staged decision. What is spoken back
  is Relay's sentence, not the model's. If the model is slow, simple
  rules answer instead.
- **Screens:** an API Gateway WebSocket API with a small dedicated Lambda
  ([`device_gateway_handler.py`](device_gateway_handler.py)) for connect,
  disconnect, register, subscribe and acknowledge. It is separate from
  Relay so Relay's IAM role stays narrow. An open page sits idle with no
  polling until Relay pushes to it. A closed page that was registered for
  alerts gets a Web Push notification signed with a VAPID key held in
  Parameter Store, and Relay holds the findings for it. A page
  re-registers every five minutes. A registration older than two hours,
  or silent for fifteen minutes, is treated as gone. Every push has a
  time cap so a dead connection can't use up the skill's few seconds.
- **Echo alerts:** the Alexa Proactive Events API, with the skill's
  credentials in Parameter Store. SHIP sends one notification per new
  finding.
- **Observability:** Relay writes one audit line per decision and per
  refusal to CloudWatch. It carries the tool, outcome, pull request,
  finding ids and a reason code, never the reason or finding text.
- **IAM:** reference policies for each Lambda role are in
  [`infra/iam/`](infra/iam/). They are not applied by the repo.

### Relay's tools

Relay is the MCP server. Each tool declares the surface its output is for,
and the server enforces it. A spoken sentence is capped at 180 characters
with no line breaks, so a list of findings can't be spoken.

| Tool | Surface | What it does |
|---|---|---|
| `release_status` | Voice | One sentence from the precomputed summary: counts, where to look, and which screens exist. |
| `blocked_pull_requests` | Screen | Every pull request with open findings, numbered. Records them as shown. |
| `finding_detail` | Screen | One finding in full. Records it as shown. |
| `display_on` | Action | Pushes the findings to a named screen, or sends a notification if the page is closed. Reports plainly when neither is possible. |
| `connected_screens` | Action | Screens that are open now or registered for alerts. |
| `findings_routing` | Action | Which findings were shown, and under what number. Records nothing. |
| `stage_decisions` | Action | Validates a batch of decisions for one pull request and stages it. Each item is `{alert_id, decision, reason}`, and `decision` must be exactly `allow` or `block`. Anything else, including an `approved` flag, is refused. Applies nothing. |
| `proceed` | Action | Applies the staged batch after re-checking each finding. Reports each outcome: applied, skipped, already decided, not found, unreadable or error. |
| `cancel_pending` | Action | Drops the staged batch. |
| `request_risk_acceptance` | Action | Allows one finding in a single step under the same two server rules. It can't block. The skill's model can't reach it. |

## Setup

Deploying this yourself needs an AWS account (Bedrock, Lambda, DynamoDB,
SQS, API Gateway) and a GitHub repository to protect. Step-by-step
walkthrough, including troubleshooting for a brand-new AWS account:
[SETUP.md](SETUP.md). [`deploy.sh`](deploy.sh) scripts part of it.
Reference IAM policies: [`infra/iam/`](infra/iam/).

## Project structure

```
src/
  agents/
    screener.py        # Fast regex/AST pre-filter + per-fragment isolation
    detector.py        # Strands Agent, RAG-grounded semantic judgment
    triage.py          # Risk-based routing, per-category thresholds
  api/
    main.py             # Webhook route, fast-ack, fragment dispatch, status lifecycle
    dashboard.py        # Gate, the human review console
    alexa_notify.py     # Sends the Echo notification when a finding is recorded
    github_client.py    # PR-diff fetching via the GitHub API
    github_writeback.py # Comments and the ship/compliance commit status
  aws/
    bedrock_session.py  # Shared Bedrock session + adaptive-retry config
  rag/
    chunker.py          # Regulation text -> citable paragraph chunks
    vector_store.py     # Local embedding store + retrieval
    embedding_cache.py  # Cached corpus embeddings
    pattern_store.py    # Learned patterns for Detector
  storage/
    alert_store.py      # DynamoDB: idempotent alerts, decisions, held blocks
    repo_store.py       # DynamoDB: which repos SHIP watches
    status_store.py     # DynamoDB: precomputed release summary
    review_ledger.py    # Which fragments of a commit's review have reported
    session_store.py    # Which finding was shown, when, under what number
    pending_store.py    # Decisions staged by voice
    device_store.py     # Open screens by name
    push_store.py       # Browsers registered for alerts, findings held for them
    alexa_user_store.py # Which Alexa users SHIP may notify
  taxonomy.py           # Single source of truth: detector <-> corpus <-> Screener trigger mapping
  relay/
    server.py           # Relay, the MCP server and its tools
    modality.py         # Enforces the spoken-response cap
    readmodel.py        # Voice path (summary) vs. screen path (alerts)
    push.py             # Delivers a payload to a named screen over its open connection
    webpush.py          # Notifies a screen whose page is closed
    audit.py            # One audit line per decision and refusal
  dashboard_ui/templates/  # Gate pages
alexa-skill/
  lambda/lambda_function.py  # The Alexa Skill: Alexa intents -> Relay over MCP
  lambda/orchestrator.py     # Free speech: the model proposes, plain code checks
  skill-package/             # Skill manifest and language model
infra/iam/                 # Reference IAM policies, one per Lambda role
rag_corpus/                # Sourced regulation/standard text, verbatim
shipagentcore/             # AgentCore Runtime deployment of Detector
docs/
  alexa-simulator.html     # Web simulator; an MCP client of Relay
  ship-mcp-client.js       # The MCP client the simulator uses: initialize, tools/list, tools/call
  ship-display.html        # Screen page: names itself, receives findings, turns on alerts
  ship-render.js           # Findings table shared by both pages above
  ship-sw.js               # Service worker that shows the alert notification
  architecture.html        # Full pipeline diagram
  voice-resolution-workflow.md  # How a spoken decision is checked
scripts/
  healing_loop.py          # Periodic corpus-grounding check
  pattern_miner.py         # Extracts patterns from allowed findings
  benchmark_models.py      # The benchmark behind the Nova Lite choice
  precompute_embeddings.py # Regenerates rag_corpus/'s cached embeddings
lambda_handler.py          # Webhook Lambda entrypoint
fragment_lambda_handler.py # Fragment-processor Lambda entrypoint
relay_lambda_handler.py    # Relay entrypoint: builds a fresh app per invocation
device_gateway_handler.py  # WebSocket connect/disconnect/register/subscribe/ack entrypoint
tests/                     # Test suite, runs without AWS credentials
```

## Status & what's next

The review pipeline, Gate, Relay, the Alexa Skill, the web simulator and
screen delivery (open pages and Web Push) are built.

What's not built yet:
- **Auto-applying the suggested fix.** SHIP drafts the patch but doesn't
  push it to the PR. A person applies it after reviewing it in Gate.
  A GitHub-API integration away, not an architecture change.
- **Scheduling the Healing Loop and the pattern miner.**
  [Healing Loop](scripts/healing_loop.py) re-checks that sourced
  regulation text still matches the law. The pattern miner turns allowed
  findings into patterns. A blocked finding is never mined, and a pattern
  reaches Detector only after three independent decisions, as context and
  never as a rule. Both run as manual scripts, not on a schedule or alert
  channel.
- **A dead-letter consumer.** A review whose queue message exhausts its
  retries leaves the check pending, which is the safe direction. A
  consumer would turn that into an explicit error status.
- **The Alexa+ add-on itself.** Add-on tooling is limited to select partners
  today. When it opens, the add-on points at the same Relay server and the
  skill steps aside.
- **Detectors that need more than a single PR diff.** Infrastructure or
  deployment context, or behavior observed across multiple files or over
  time. See [architecture.html](https://pandayv.github.io/ship-engine/architecture.html)
  for where that boundary sits.

## License

MIT. See [`LICENSE`](LICENSE).
