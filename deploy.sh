#!/usr/bin/env bash
# Wraps SETUP.md's steps 3-8 into one script: confirms Bedrock access,
# creates the DynamoDB tables, deploys Detector to AgentCore, creates the
# fragment queue + DLQ + fragment-processor Lambda, and creates the
# webhook Lambda. Steps 1 (clone) and 2 (RAG corpus, already checked in)
# need nothing. Step 9 (verify) is a manual check after this finishes.
#
# The IAM policies this script writes are freshly scoped to exactly what
# each role needs, derived from SETUP.md's own step-by-step requirements,
# not copied from any existing deployment. Read them before running this
# against an account that matters (see the POLICY comments below).
#
# Usage: ./deploy.sh
# Prompts for anything it can't safely generate or infer.

set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)
step() { echo "${BOLD}==> $1${RESET}"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

command -v aws >/dev/null || fail "AWS CLI not found. Install it first."
command -v python3 >/dev/null || fail "python3 not found."
command -v npm >/dev/null || fail "npm not found (needed for the AgentCore CLI)."

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text) \
  || fail "aws sts get-caller-identity failed. Is 'aws configure' set up with a scoped IAM identity?"
REGION=$(aws configure get region) || fail "No default region set. Run 'aws configure' first."
echo "AWS account: $ACCOUNT_ID   Region: $REGION"
read -rp "Continue deploying SHIP into this account/region? [y/N] " CONFIRM
[[ "$CONFIRM" =~ ^[Yy]$ ]] || exit 0

# ---------------------------------------------------------------------------
step "Step 3: Confirm Bedrock access works before deploying anything"
# ---------------------------------------------------------------------------
export SHIP_MODEL_BACKEND=bedrock
export SHIP_DETECTOR_MODE=in_process
if ! python3 -m src.agents.detector >/tmp/ship-bedrock-check.log 2>&1; then
  cat /tmp/ship-bedrock-check.log
  fail "Bedrock check failed. Enable model access in the Bedrock console \
(Model access -> request access) — a brand-new account may need a support \
case to raise its quota. This is an account-activation gate, not a script bug."
fi
echo "Bedrock access confirmed."

# ---------------------------------------------------------------------------
step "Step 4: Create the DynamoDB tables"
# ---------------------------------------------------------------------------
python3 -m src.storage.alert_store
python3 -m src.storage.repo_store
echo "Tables created (or already existed)."

# ---------------------------------------------------------------------------
step "Step 5: Deploy Detector to a real AgentCore Runtime"
# ---------------------------------------------------------------------------
npm list -g @aws/agentcore >/dev/null 2>&1 || npm install -g @aws/agentcore
pushd shipagentcore >/dev/null
AGENTCORE_OUT=$(agentcore deploy --yes)
popd >/dev/null
echo "$AGENTCORE_OUT"
AGENTCORE_ARN=$(echo "$AGENTCORE_OUT" | grep -oE 'arn:aws:bedrock-agentcore:[^ ]+' | head -1)
[[ -n "$AGENTCORE_ARN" ]] || { echo "$AGENTCORE_OUT"; read -rp "Paste the AgentCore runtime ARN from the output above: " AGENTCORE_ARN; }
echo "Runtime ARN: $AGENTCORE_ARN"

# ---------------------------------------------------------------------------
step "Step 6: Fragment queue, DLQ, and the fragment-processor Lambda"
# ---------------------------------------------------------------------------
aws sqs create-queue --queue-name ship-fragment-queue-dlq \
  --attributes MessageRetentionPeriod=1209600 >/dev/null 2>&1 || true
DLQ_URL=$(aws sqs get-queue-url --queue-name ship-fragment-queue-dlq --query QueueUrl --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

aws sqs create-queue --queue-name ship-fragment-queue \
  --attributes "{\"VisibilityTimeout\":\"960\",\"RedrivePolicy\":\"{\\\"deadLetterTargetArn\\\":\\\"$DLQ_ARN\\\",\\\"maxReceiveCount\\\":3}\"}" \
  >/dev/null 2>&1 || true
FRAGMENT_QUEUE_URL=$(aws sqs get-queue-url --queue-name ship-fragment-queue --query QueueUrl --output text)
FRAGMENT_QUEUE_ARN=$(aws sqs get-queue-attributes --queue-url "$FRAGMENT_QUEUE_URL" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

# POLICY: fragment-processor role. Needs to consume the fragment queue,
# invoke the AgentCore runtime from step 5, and read/write ship-alerts,
# ship-repos, ship-status, ship-recent-reviews, ship-learned-patterns.
cat > /tmp/ship-trust-policy.json <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
cat > /tmp/ship-fragment-processor-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"], "Resource": "arn:aws:logs:$REGION:$ACCOUNT_ID:*"},
    {"Effect": "Allow", "Action": ["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes"], "Resource": "$FRAGMENT_QUEUE_ARN"},
    {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime"], "Resource": "$AGENTCORE_ARN"},
    {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan"],
     "Resource": [
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-alerts",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-repos",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-status",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-recent-reviews",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-learned-patterns",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-device-connections"
     ]}
  ]
}
EOF
aws iam create-role --role-name ship-fragment-processor-role \
  --assume-role-policy-document file:///tmp/ship-trust-policy.json >/dev/null 2>&1 || true
aws iam put-role-policy --role-name ship-fragment-processor-role \
  --policy-name ship-fragment-processor-policy \
  --policy-document file:///tmp/ship-fragment-processor-policy.json
FRAGMENT_ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/ship-fragment-processor-role"
echo "Waiting for IAM role propagation..."
sleep 10

# Build the shared deployment zip (used by both Lambdas).
rm -rf build ship-webhook.zip
mkdir -p build
cp -r src fragment_lambda_handler.py lambda_handler.py build/
pip install -r requirements-lambda.txt -t build/ \
  --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.12 --quiet
(cd build && zip -rq ../ship-webhook.zip . -x "*.dist-info/*")
echo "Deployment zip built: ship-webhook.zip"

aws lambda create-function --function-name ship-fragment-processor \
  --runtime python3.12 --architectures arm64 \
  --role "$FRAGMENT_ROLE_ARN" \
  --handler fragment_lambda_handler.handler \
  --timeout 900 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={SHIP_DETECTOR_MODE=agentcore,SHIP_MODEL_BACKEND=bedrock,SHIP_AGENTCORE_RUNTIME_ARN=$AGENTCORE_ARN}" \
  >/dev/null 2>&1 || aws lambda update-function-code --function-name ship-fragment-processor \
  --zip-file fileb://ship-webhook.zip >/dev/null

aws lambda create-event-source-mapping --function-name ship-fragment-processor \
  --event-source-arn "$FRAGMENT_QUEUE_ARN" --batch-size 1 \
  --scaling-config MaximumConcurrency=5 >/dev/null 2>&1 || true
echo "Fragment-processor Lambda deployed."

# ---------------------------------------------------------------------------
step "Step 7: Deploy the webhook Lambda"
# ---------------------------------------------------------------------------
read -rp "GitHub personal access token (read access to the target repo): " GITHUB_TOKEN
GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
SHIP_DASHBOARD_TOKEN=$(openssl rand -hex 32)
echo "Generated GITHUB_WEBHOOK_SECRET and SHIP_DASHBOARD_TOKEN (saved below, keep them safe)."

# POLICY: webhook role. Needs to send to the fragment queue, and (for the
# no-queue-configured inline fallback) the same DynamoDB/Bedrock access as
# the fragment-processor role above.
cat > /tmp/ship-webhook-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"], "Resource": "arn:aws:logs:$REGION:$ACCOUNT_ID:*"},
    {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": "$FRAGMENT_QUEUE_ARN"},
    {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime"], "Resource": "$AGENTCORE_ARN"},
    {"Effect": "Allow", "Action": ["bedrock:InvokeModel"], "Resource": "*"},
    {"Effect": "Allow", "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query","dynamodb:Scan"],
     "Resource": [
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-alerts",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-repos",
       "arn:aws:dynamodb:$REGION:$ACCOUNT_ID:table/ship-status"
     ]}
  ]
}
EOF
aws iam create-role --role-name ship-webhook-role \
  --assume-role-policy-document file:///tmp/ship-trust-policy.json >/dev/null 2>&1 || true
aws iam put-role-policy --role-name ship-webhook-role \
  --policy-name ship-webhook-policy \
  --policy-document file:///tmp/ship-webhook-policy.json
WEBHOOK_ROLE_ARN="arn:aws:iam::$ACCOUNT_ID:role/ship-webhook-role"
echo "Waiting for IAM role propagation..."
sleep 10

aws lambda create-function --function-name ship-webhook \
  --runtime python3.12 --architectures arm64 \
  --role "$WEBHOOK_ROLE_ARN" \
  --handler lambda_handler.handler \
  --timeout 30 --memory-size 512 \
  --zip-file fileb://ship-webhook.zip \
  --environment "Variables={SHIP_DETECTOR_MODE=agentcore,SHIP_MODEL_BACKEND=bedrock,SHIP_AGENTCORE_RUNTIME_ARN=$AGENTCORE_ARN,SHIP_FRAGMENT_QUEUE_URL=$FRAGMENT_QUEUE_URL,GITHUB_TOKEN=$GITHUB_TOKEN,GITHUB_WEBHOOK_SECRET=$GITHUB_WEBHOOK_SECRET,SHIP_DASHBOARD_TOKEN=$SHIP_DASHBOARD_TOKEN}" \
  >/dev/null 2>&1 || aws lambda update-function-code --function-name ship-webhook \
  --zip-file fileb://ship-webhook.zip >/dev/null

FUNCTION_URL=$(aws lambda create-function-url-config --function-name ship-webhook \
  --auth-type NONE --query FunctionUrl --output text 2>/dev/null || \
  aws lambda get-function-url-config --function-name ship-webhook --query FunctionUrl --output text)
echo "Webhook Lambda deployed."

# ---------------------------------------------------------------------------
step "Done. What's left is two manual steps (step 8 and step 9 in SETUP.md):"
# ---------------------------------------------------------------------------
cat <<EOF

1. Connect your repo:
   Open: ${FUNCTION_URL}dashboard/repos?token=$SHIP_DASHBOARD_TOKEN
   and connect <owner>/<repo>.

2. Point a real GitHub webhook at it:
   In the target repo's Settings -> Webhooks, add:
     Payload URL: $FUNCTION_URL
     Content type: application/json
     Secret: $GITHUB_WEBHOOK_SECRET
     Event: Pull requests

Dashboard token (keep this secret): $SHIP_DASHBOARD_TOKEN
Webhook secret (keep this secret):  $GITHUB_WEBHOOK_SECRET

Verify: curl ${FUNCTION_URL}health
EOF
