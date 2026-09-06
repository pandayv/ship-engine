# SHIP

**An autonomous compliance gatekeeper for AI-feature code changes.** A
GitHub webhook scans a pull request diff, detects AI-specific compliance
risk, verifies its own finding before trusting it, cites the actual
regulation it violates, and drafts a remediation patch — then freezes the
build and hands a human the decision, never resolving a high-risk case on
its own.

Built for the [Agents for Humans Hackathon](https://agentsforhumans.devpost.com/)
(Strands Agents SDK, Professional Agents track); the same engine is the
foundation for a second phase (Amazon AppDev 2026) that adds a voice/screen
surface on top without changing anything described here.

---

## The problem

Every fintech startup is racing to bolt AI onto its product. Almost none
of them have someone whose job is to notice when that AI quietly breaks
the law.

An LLM-assisted underwriting opinion. An automated risk score. A chatbot
with account access. Each one ships in an afternoon — and each one can
just as easily leak a customer's SSN into a prompt, let a zip code quietly
decide who gets a loan, or let an AI's opinion become the final answer
with nobody signing off. GDPR fines reach €20 million or 4% of global
revenue, whichever is bigger. The EU AI Act adds a second, dedicated
exposure specifically for credit-scoring AI, classified as high-risk
outright. And the five-person engineering team that just shipped the
feature has no compliance hire, no legal review queue, and no time to
build one.

The usual answers both fail: ship blind and hope, or slow every single PR
down for a human review that doesn't scale and that nobody actually wants
to do all day. A generic static-analysis tool doesn't help either — it
has never heard of an LLM call and wouldn't know a compliance violation
from a syntax error.

SHIP is the missing hire, running on every pull request instead of once a
quarter. It reads the diff the moment a PR opens, judges whether it's
actually a violation — not just a keyword match — cites the exact
regulation it breaks, drafts the fix, and only ever interrupts a human
when it's found something real. Everything else ships without anyone
ever seeing it.

## Guiding principles

### Trust, but verify
- Diagnostician's every citation is grounded in retrieved regulation text
  (GDPR, EU AI Act, OWASP), never a model's unaided recollection — it must
  call its retrieval tool before judging anything.
- Screener's keyword match is a *signal*, not a verdict — Diagnostician
  independently judges whether it's a real violation or a false positive,
  verified against both genuine violations and deliberate look-alikes
  designed to trip a naive pattern match, not just the easy cases.
- The stricter categories carry an explicit evidence bar in the prompt
  itself — a protected characteristic must connect to an actual scoring
  *operation* (an arithmetic adjustment, a conditional), not just appear
  in the same function, so a field that's merely present isn't confused
  with a field that's actually driving the decision.

### A human always makes the call
- Triage only ever proposes an action — log-and-continue, or freeze. It
  never resolves anything by itself.
- Attending (the review dashboard, gated behind its own credential
  separate from the webhook's) is the only place a frozen alert gets
  resolved. Approve/Reject is a one-way, guarded transition: a retry or a
  duplicate webhook delivery cannot silently re-open or overwrite a
  decision a human already made.

### AI-specific scope, on purpose
The pipeline underneath is mechanically generic enough to flag other
things too, but SHIP deliberately stays scoped to AI-feature risk
(raw data reaching a model, an AI output driving a decision with no
checkpoint, a protected characteristic feeding a score, an agent granted
an unscoped dangerous capability). That scope *is* the product — diluting
it into general-purpose static analysis would trade away the one thing
that differentiates this from tools that already exist.

### Built to survive real traffic, not just a demo
A webhook redelivery or a retried job can never create a duplicate alert,
and can never silently reopen or overwrite a decision a human already
made. Every flagged issue in a PR is reviewed independently and in
parallel, so one slow or unlucky issue can never crowd out the review of
another in the same PR (see [Architecture](#architecture) below). Every
property on this list is verified against the real, deployed system, not
asserted from a passing test suite alone.

## What it does

1. **Screener** — a fast, free regex/AST pre-filter. Runs on every commit;
   if nothing matches, the PR passes in milliseconds and never costs a
   model call. A match doesn't mean a violation — it means "worth a real
   look."
2. **Diagnostician** — a Strands Agent, RAG-grounded against real,
   sourced regulation text. Runs only on the fragments Screener actually
   flagged, one fragment at a time, and returns a structured verdict:
   matched or not, which category, a 1–10 risk score, a plain-English
   explanation, the exact citation, and a draft remediation patch.
3. **Triage** — routes the verdict. Below the threshold: logged, nothing
   else happens. At or above it: frozen — an alert is created, pending
   human review. The threshold is per-category, not one number for
   everything — a confirmed violation that breaks a required safety
   guarantee (unmasked PII reaching an external service, an automated
   decision with no human checkpoint at all) is held to a lower bar than
   one that's more a matter of degree. (Today, "frozen" means an alert on
   the dashboard — it doesn't yet set a GitHub check that blocks the PR's
   own merge button; see [Status & what's next](#status--whats-next).)
4. **Attending** — the review console. Every frozen alert shows the file,
   the category, the risk score, the plain-English summary, the exact
   regulatory citation, and the suggested patch. A human clicks Approve or
   Reject; that decision is recorded as the one-way resolution of the
   alert. (Actually pushing the approved patch back to the PR via the
   GitHub API is a scoped-out next step, not yet wired in — today, a
   human still applies the fix themselves once they've reviewed it here.)

### Detectors

Each independently verified against real, deliberately adversarial test
cases — genuine violations and deliberate false-positive look-alikes both.

| ID | What it catches | Grounded in |
|---|---|---|
| **PIIE-001** | Raw, direct PII (SSN, account number, full profile) reaching an external sink with no masking | GDPR Article 32 |
| **PIIE-002** | The same kind of raw PII, written to a log stream | GDPR Article 32 |
| **PIIE-003** | Raw PII stored in a cache/session store with no encryption | GDPR Article 32 |
| **TLGP-002** | An AI-produced decision applied as final with no human checkpoint anywhere in the fragment | EU AI Act Article 14 |
| **ALBP-001** | A protected characteristic (or a clear proxy) directly driving a scoring calculation | EU AI Act Article 10 + Annex III §5(b) |
| **TLGP-001** | A dangerous capability (shell exec, unscoped DB write) granted to an AI agent with no gate | OWASP Top 10 for LLM Apps, LLM06:2025 |

SHIP is deliberately scoped to what a single PR diff can actually prove —
see the architecture doc for the reasoning behind that boundary, and
what's on the roadmap next.

## See it in action

The engine is demonstrated against a fork of
[MicroPyramid/micro-finance](https://github.com/MicroPyramid/micro-finance)
(MIT-licensed, a real Django lending app) — used purely as a realistic
third-party target, kept as a fully separate repository from this
submission, nothing from it incorporated here:

- **[PR #1](https://github.com/pandayv/micro-finance/pull/1)** plants an
  AI-assisted underwriting function sending an applicant's full raw
  profile to an external LLM.
- **[PR #2](https://github.com/pandayv/micro-finance/pull/2)** plants one
  genuine violation per remaining detector across three new files,
  interleaved with four deliberate false-positive look-alikes (a
  non-agent backup job, a display-only profile field, a properly-hashed
  log call, a non-PII cache write).

Run directly against real Bedrock, both PRs together: **9 for 9** — every
genuine violation correctly caught with an accurate citation, every
look-alike correctly dismissed with real reasoning for why, not a
coin-flip.

## Architecture

Full diagram and component-by-component detail: [pandayv.github.io/ship-engine](https://pandayv.github.io/ship-engine/architecture.html) ([source](docs/architecture.html)).

The one piece worth calling out here: **each flagged issue in a PR is
reviewed as its own independent, retryable job**, queued and picked up by
an independently-scaling reviewer function, rather than one sequential
pass through the whole PR. A fragment that fails outright retries
automatically and lands in a dead-letter queue after repeated failure
instead of vanishing; a fragment that's just slow, or that hits AWS's own
request-rate limit, backs off and retries on its own without blocking
anything else. A PR with several flagged issues takes about as long as
its slowest single issue, not the sum of all of them, and nothing is
silently dropped.

## Tech stack

- **Agent framework:** [Strands Agents SDK](https://github.com/strands-agents/sdk-python)
- **Models:** Amazon Bedrock (Claude Haiku 4.5) as the primary backend;
  Google Gemini as a credit-exhaustion fallback; a local Ollama model as a
  fully-offline reliability fallback — switchable via one env var, all
  three genuinely functional, not just Bedrock with the others as
  unverified stretch goals
- **Agent runtime:** Amazon Bedrock AgentCore Runtime — the same
  Diagnostician logic deployed to a real managed runtime
  ([`shipagentcore/`](shipagentcore/)), callable in-process for local
  development or remotely for the deployed path, toggled the same way
- **Retrieval:** Amazon Bedrock Titan Embeddings, a small local
  numpy cosine-similarity store (the sourced regulation corpus is a
  few dozen chunks — a hosted vector database would be pure overhead
  at this scale)
- **Compute:** two AWS Lambda functions — the webhook (fast-ack: verify,
  screen, dispatch) and an independently-scaling fragment processor
  (the actual model call, one fragment per invocation)
- **Queueing:** Amazon SQS, with a dead-letter queue for fragments that
  fail repeatedly and a concurrency cap on the processor so parallel
  reviews stay within the account's real request-rate limit
- **State:** Amazon DynamoDB — every alert write is idempotent (a webhook
  redelivery or a retried job can't create a duplicate, and can't silently
  re-open a decision a human already made)
- **Web:** FastAPI (the webhook route and the Attending dashboard, one
  deployable app, wrapped for Lambda via Mangum)

## Setting this up yourself

### What you need

- An AWS account with Bedrock model access enabled for at least one
  Claude model (a brand-new account may need a one-time use-case
  submission and/or an AWS Support request before this works — see
  the troubleshooting note below if `bedrock:InvokeModel` fails with a
  quota or subscription error).
- The AWS CLI, configured (`aws configure`) with a scoped IAM identity —
  not root credentials.
- Python 3.12+, `pip`, and `npm` (for the AgentCore CLI).
- A GitHub repo to protect, and a personal access token with read access
  to it.

### 1. Clone and set up the local environment

```bash
git clone https://github.com/pandayv/ship-engine.git
cd ship-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Source the RAG corpus

The regulation text this project cites is already checked into
[`rag_corpus/`](rag_corpus/) (GDPR Art. 32, EU AI Act Art. 10/14 + Annex
III §5(b), OWASP LLM06:2025) — sourced verbatim, not paraphrased. Nothing
to do here unless you're extending the taxonomy with a new category; see
[`src/taxonomy.py`](src/taxonomy.py) for the registry a new category needs
to join.

### 3. Confirm Bedrock actually works before deploying anything

```bash
export SHIP_MODEL_BACKEND=bedrock
export SHIP_DIAGNOSTICIAN_MODE=in_process
python3 -m src.agents.diagnostician
```

This runs a hardcoded violation through the real agent end to end and
prints the structured verdict — the fastest way to find out if Bedrock
access, model availability, or region config needs fixing before anything
else. If it fails with an access or quota error, the model needs enabling
in the Bedrock console first (Model access → request access), and a
brand-new AWS account specifically may need its own quota raised via a
support case — this is an account-activation gate, not a code problem.

### 4. Create the DynamoDB table

```bash
python3 -m src.storage.alert_store
```

Running this module directly calls `create_table_if_not_exists()` and
then a self-test write/read/resolve cycle against the real table — needs
`dynamodb:CreateTable` plus the basic item-level actions on your IAM
identity first.

### 5. Deploy Diagnostician to a real AgentCore Runtime

```bash
npm install -g @aws/agentcore
cd shipagentcore
agentcore deploy --yes
```

Note the deployed runtime ARN from the output — you'll need it in step 7.
**If Diagnostician's prompt, schema, or RAG corpus ever changes, both
`src/agents/diagnostician.py` and `shipagentcore/app/ship_diagnostician/main.py`
need the same change and a fresh deploy** — see
[`tests/test_taxonomy_consistency.py`](tests/test_taxonomy_consistency.py),
which exists specifically to catch the two copies drifting apart before
that reaches production silently.

### 6. Create the fragment queue, its dead-letter queue, and the fragment-processor Lambda

```bash
aws sqs create-queue --queue-name ship-fragment-queue-dlq \
  --attributes MessageRetentionPeriod=1209600

DLQ_ARN=$(aws sqs get-queue-attributes \
  --queue-url "$(aws sqs get-queue-url --queue-name ship-fragment-queue-dlq --query QueueUrl --output text)" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

aws sqs create-queue --queue-name ship-fragment-queue \
  --attributes "{\"VisibilityTimeout\":\"960\",\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":3}\"}"
```

The fragment-processor Lambda needs its own execution role (permission to
consume the queue, call `bedrock-agentcore:InvokeAgentRuntime` against
the ARN from step 5, and write to the DynamoDB table from step 4).

Both Lambdas in this project deploy from the same zip — build it once,
including real Linux dependency wheels (the `--platform`/`--only-binary`
flags matter even if you're building on macOS/Windows; skip them and the
zip will contain the wrong platform's compiled packages and fail at
import time on Lambda, not at build time):

```bash
mkdir -p build && cp -r src fragment_lambda_handler.py lambda_handler.py build/
pip install -r requirements-lambda.txt -t build/ \
  --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12
cd build && zip -r ../ship-webhook.zip . -x "*.dist-info/*" && cd ..
```

(Building for `arm64`/`manylinux2014_aarch64` above to match Lambda's
cheaper Graviton architecture — switch both the `--platform` flag here and
`--architectures` below to `x86_64` consistently if you'd rather not deal
with cross-compiling.)

```bash
aws lambda create-function --function-name ship-fragment-processor \
  --runtime python3.12 --architectures arm64 \
  --role <YOUR_FRAGMENT_PROCESSOR_ROLE_ARN> \
  --handler fragment_lambda_handler.handler \
  --timeout 900 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={SHIP_DIAGNOSTICIAN_MODE=agentcore,SHIP_MODEL_BACKEND=bedrock,SHIP_AGENTCORE_RUNTIME_ARN=<ARN_FROM_STEP_5>}"

aws lambda create-event-source-mapping --function-name ship-fragment-processor \
  --event-source-arn <FRAGMENT_QUEUE_ARN> --batch-size 1 \
  --scaling-config MaximumConcurrency=5
```

`MaximumConcurrency` is the knob that keeps parallel fragment reviews
within your account's real Bedrock request-rate limit — raise it once you
know what that limit actually is for your account.

### 7. Deploy the webhook Lambda

Needs its own execution role (permission to send to the queue from step
6, and — if you also want the fast-ack path itself to be able to fall
back to processing inline with no queue configured — the same Bedrock/
DynamoDB permissions as step 6's role):

```bash
aws lambda create-function --function-name ship-webhook \
  --runtime python3.12 --architectures arm64 \
  --role <YOUR_WEBHOOK_ROLE_ARN> \
  --handler lambda_handler.handler \
  --timeout 30 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={
    SHIP_DIAGNOSTICIAN_MODE=agentcore,
    SHIP_MODEL_BACKEND=bedrock,
    SHIP_AGENTCORE_RUNTIME_ARN=<ARN_FROM_STEP_5>,
    SHIP_FRAGMENT_QUEUE_URL=<QUEUE_URL_FROM_STEP_6>,
    SHIP_ALLOWED_REPOS=<owner>/<repo>,
    GITHUB_TOKEN=<a token with read access to that repo>,
    GITHUB_WEBHOOK_SECRET=<a random secret you generate>,
    SHIP_DASHBOARD_TOKEN=<a second random secret you generate>
  }"

aws lambda create-function-url-config --function-name ship-webhook \
  --auth-type NONE
```

`SHIP_ALLOWED_REPOS` is a hard allowlist — a payload naming any other repo
gets rejected before it can spend your GitHub token or Bedrock quota.
`GITHUB_WEBHOOK_SECRET` and `SHIP_DASHBOARD_TOKEN` must be two genuinely
different values — the webhook's signature check and the dashboard's auth
are deliberately independent, so compromising one can't silently disable
the other.

### 8. Point a real GitHub webhook at it

In the target repo's Settings → Webhooks: the Lambda Function URL from
step 7, content type `application/json`, secret matching
`GITHUB_WEBHOOK_SECRET` above, event: Pull requests.

### 9. Verify

```bash
curl https://<your-function-url>/health
# {"status":"ok"}
```

Open a real PR against the target repo containing something Screener
would flag (raw PII reaching an external call is the easiest to trigger)
and confirm an alert appears at
`https://<your-function-url>/dashboard?token=<SHIP_DASHBOARD_TOKEN>`.

## Project structure

```
src/
  agents/
    screener.py        # Fast regex/AST pre-filter + per-fragment isolation
    diagnostician.py    # Strands Agent — RAG-grounded semantic judgment
    triage.py           # Risk-based routing, per-category thresholds
  api/
    main.py             # Webhook route, fast-ack + fragment dispatch
    dashboard.py         # Attending — the human review console
    github_client.py    # Real PR-diff fetching via the GitHub API
  aws/
    bedrock_session.py  # Shared Bedrock session + adaptive-retry config
  rag/
    chunker.py           # Regulation text -> citable paragraph chunks
    vector_store.py      # Local embedding store + retrieval
  storage/
    alert_store.py       # DynamoDB — idempotent alert persistence
  taxonomy.py            # Single source of truth: detector <-> corpus <-> Screener trigger mapping
  mcp/                   # Placeholder for Herald (Phase 2, not yet built)
rag_corpus/               # Sourced regulation/standard text, verbatim
shipagentcore/             # AgentCore Runtime deployment of Diagnostician
lambda_handler.py          # Webhook Lambda entrypoint
fragment_lambda_handler.py # Fragment-processor Lambda entrypoint
tests/                     # 88 tests, no AWS credentials required to run
```

## Status & what's next

Built for Sep 14, 2026 (Agents for Humans): the full pipeline above, live
and deployed, verified end to end against real Bedrock and a real GitHub
webhook — not a mocked demo. A second phase (Amazon AppDev 2026, Oct 23)
adds **Herald**, an MCP server exposing the same underlying state (active
blockers, violation details, patch application) as discoverable tools for
a voice or screen surface — no rewrite of anything above, purely additive.

Three things known and deliberately not built yet, not overlooked:
- **Freezing an alert doesn't yet block the PR's own merge button in
  GitHub** — it creates a dashboard alert a human is expected to check
  before merging, but nothing here sets a GitHub commit status or
  required check today. Approving an alert also doesn't yet push the
  suggested patch back to the PR automatically; a human still applies it.
  Both are a real GitHub-API integration away, not an architecture change.
- A background job that keeps the RAG corpus current as the underlying
  regulations change (today's corpus is accurate as sourced, not
  self-updating).
- New detectors, beyond what's listed above, for risk patterns that need
  more than a single PR diff to prove — infrastructure/deployment context,
  or behavior observed across multiple files or over time. See the
  architecture doc for where that boundary sits and why.

## License

MIT — see [`LICENSE`](LICENSE).
