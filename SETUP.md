# Deploying SHIP yourself

Full step-by-step walkthrough for standing up a real deployment: your own
AWS account, your own GitHub repo, your own Gate dashboard. Takes about
30–45 minutes for a first bring-up.

**In a hurry?** [`deploy.sh`](deploy.sh) runs steps 3–7 below for you.
That includes creating the two IAM roles steps 6 and 7 otherwise leave
as placeholders (`<YOUR_..._ROLE_ARN>`), and generating both secrets. It
still stops for the two manual steps at the end, 8 and 9, connecting
your repo and pointing GitHub's webhook at it, since those need a
browser and your repo's settings page. Read the script before running
it against an account that matters, it creates real IAM roles and
Lambda functions.

## What you need

- An AWS account with Bedrock model access enabled for at least one
  Claude model (a brand-new account may need a one-time use-case
  submission and/or an AWS Support request before this works. See
  the troubleshooting note below if `bedrock:InvokeModel` fails with a
  quota or subscription error).
- The AWS CLI, configured (`aws configure`) with a scoped IAM identity,
  not root credentials.
- Python 3.12+, `pip`, and `npm` (for the AgentCore CLI).
- A GitHub repo to protect, and a personal access token with read access
  to it.

## 1. Clone and set up the local environment

```bash
git clone https://github.com/pandayv/ship-engine.git
cd ship-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Source the RAG corpus

The regulation text this project cites is already checked into
[`rag_corpus/`](rag_corpus/) (GDPR Art. 32, EU AI Act Art. 10/14 + Annex
III §5(b), OWASP LLM06:2025), sourced verbatim, not paraphrased. There's
nothing to do here unless you're extending the taxonomy with a new
category. [`src/taxonomy.py`](src/taxonomy.py) has the registry a new
category needs to join.

## 3. Confirm Bedrock actually works before deploying anything

```bash
export SHIP_MODEL_BACKEND=bedrock
export SHIP_DETECTOR_MODE=in_process
python3 -m src.agents.detector
```

This runs a hardcoded violation through the real agent end to end and
prints the structured verdict. It's the fastest way to find out if
Bedrock access, model availability, or region config needs fixing before
anything else. If it fails with an access or quota error, the model needs
enabling in the Bedrock console first (Model access → request access),
and a brand-new AWS account specifically may need its own quota raised
via a support case. This is an account-activation gate, not a code
problem.

## 4. Create the DynamoDB tables

```bash
python3 -m src.storage.alert_store
python3 -m src.storage.repo_store
```

`alert_store` calls `create_table_if_not_exists()` and then a self-test
write/read/resolve cycle against the real `ship-alerts` table.
`repo_store` creates `ship-repos`, the table backing Gate's
connected-repos view. Needs `dynamodb:CreateTable` plus the basic
item-level actions on your IAM identity first.

## 5. Deploy Detector to a real AgentCore Runtime

```bash
npm install -g @aws/agentcore
cd shipagentcore
agentcore deploy --yes
```

Note the deployed runtime ARN from the output. You'll need it in step 7.
**If Detector's prompt, schema, or RAG corpus ever changes, both
`src/agents/detector.py` and `shipagentcore/app/ship_diagnostician/main.py`
need the same change and a fresh deploy.** See
[`tests/test_taxonomy_consistency.py`](tests/test_taxonomy_consistency.py),
which exists specifically to catch the two copies drifting apart before
that reaches production silently. (The AgentCore app's folder and runtime
are named `ship_diagnostician`, not `detector`; that's internal deployment
plumbing and doesn't affect anything above.)

## 6. Create the fragment queue, its dead-letter queue, and the fragment-processor Lambda

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

Both Lambdas in this project deploy from the same zip. Build it once,
including real Linux dependency wheels: the `--platform`/`--only-binary`
flags matter even if you're building on macOS or Windows. Skip them and
the zip will contain the wrong platform's compiled packages, failing at
import time on Lambda rather than at build time.

```bash
mkdir -p build && cp -r src fragment_lambda_handler.py lambda_handler.py build/
pip install -r requirements-lambda.txt -t build/ \
  --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12
cd build && zip -r ../ship-webhook.zip . -x "*.dist-info/*" && cd ..
```

(Building for `arm64`/`manylinux2014_aarch64` above to match Lambda's
cheaper Graviton architecture. Switch both the `--platform` flag here and
`--architectures` below to `x86_64` consistently if you'd rather not deal
with cross-compiling.)

```bash
aws lambda create-function --function-name ship-fragment-processor \
  --runtime python3.12 --architectures arm64 \
  --role <YOUR_FRAGMENT_PROCESSOR_ROLE_ARN> \
  --handler fragment_lambda_handler.handler \
  --timeout 900 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={SHIP_DETECTOR_MODE=agentcore,SHIP_MODEL_BACKEND=bedrock,SHIP_AGENTCORE_RUNTIME_ARN=<ARN_FROM_STEP_5>}"

aws lambda create-event-source-mapping --function-name ship-fragment-processor \
  --event-source-arn <FRAGMENT_QUEUE_ARN> --batch-size 1 \
  --scaling-config MaximumConcurrency=5
```

`MaximumConcurrency` is the knob that keeps parallel fragment reviews
within your account's real Bedrock request-rate limit. Raise it once you
know what that limit actually is for your account.

## 7. Deploy the webhook Lambda

Needs its own execution role: permission to send to the queue from step
6, and, if you also want the fast-ack path itself to fall back to
processing inline with no queue configured, the same Bedrock/DynamoDB
permissions as step 6's role.

```bash
aws lambda create-function --function-name ship-webhook \
  --runtime python3.12 --architectures arm64 \
  --role <YOUR_WEBHOOK_ROLE_ARN> \
  --handler lambda_handler.handler \
  --timeout 30 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={
    SHIP_DETECTOR_MODE=agentcore,
    SHIP_MODEL_BACKEND=bedrock,
    SHIP_AGENTCORE_RUNTIME_ARN=<ARN_FROM_STEP_5>,
    SHIP_FRAGMENT_QUEUE_URL=<QUEUE_URL_FROM_STEP_6>,
    GITHUB_TOKEN=<a token with read access to that repo>,
    GITHUB_WEBHOOK_SECRET=<a random secret you generate>,
    SHIP_DASHBOARD_TOKEN=<a second random secret you generate>
  }"

aws lambda create-function-url-config --function-name ship-webhook \
  --auth-type NONE
```

`GITHUB_WEBHOOK_SECRET` and `SHIP_DASHBOARD_TOKEN` must be two genuinely
different values. The webhook's signature check and the dashboard's auth
are deliberately independent, so compromising one can't silently disable
the other.

## 8. Connect the repo, then point a real GitHub webhook at it

Open `https://<your-function-url>/dashboard/repos?token=<SHIP_DASHBOARD_TOKEN>`
and connect `<owner>/<repo>`. A payload naming any other repo gets
rejected before it can spend your GitHub token or Bedrock quota (see
[`src/storage/repo_store.py`](src/storage/repo_store.py)). That link only
needs to be visited once. The token in the URL establishes a session
cookie, and every page from there on (Active, History, Connected repos)
is just a normal link with no secret in it. Visiting `/dashboard` cold
prompts a login form instead. An optional
`SHIP_ALLOWED_REPOS=<owner>/<repo>` environment variable pre-seeds this
same allowlist without needing the table at all, useful for a first
bring-up before step 4's tables exist, or as a fallback if DynamoDB is
briefly unreachable.

Then, in the target repo's Settings → Webhooks: the Lambda Function URL
from step 7, content type `application/json`, secret matching
`GITHUB_WEBHOOK_SECRET` above, event: Pull requests.

## 9. Verify

```bash
curl https://<your-function-url>/health
# {"status":"ok"}
```

Open a real PR against the target repo containing something Screener
would flag (raw PII reaching an external call is the easiest to trigger)
and confirm an alert appears at
`https://<your-function-url>/dashboard?token=<SHIP_DASHBOARD_TOKEN>`.

## Optional: connect a physical screen

Alexa handles voice on its own, nothing to set up there. Getting a
finding to show up on a TV, an iPad, a laptop, or any other screen is a
separate piece SHIP built itself (Relay's push path), and it needs a
one-time step per screen, not something repeated on every use:

1. Open [`docs/ship-display.html`](docs/ship-display.html) in that
   device's browser. If you're pointing it at your own deployment
   rather than the hosted reference instance, edit the `WS_URL` constant
   near the top of the file first to your own WebSocket endpoint from
   step 6.
2. Give the screen a name when prompted (e.g. "TV", "Conference Room").
3. Leave that tab open. It sits idle, no polling, until Relay pushes a
   finding to it by that name.

Ask Alexa to show a finding on that name and it appears there live.
