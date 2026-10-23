#!/usr/bin/env bash
# deploy.sh - stands up SHIP's AWS resources from this repository.
#
#   ./deploy.sh env           # once: write .env.deploy, then fill in the values
#   ./deploy.sh all           # everything below, in order
#   ./deploy.sh --help        # the steps one by one
#
# Creates, in one account and region:
#   tables     the ten DynamoDB tables, on-demand billing
#   queues     ship-fragment-queue and its dead-letter queue ship-fragment-queue-dlq
#   roles      four Lambda execution roles, each with its policy from infra/iam/
#   packages   the four zips in build/, through scripts/build_packages.py
#   functions  ship-relay (SnapStart, alias "live"), ship-webhook, ship-fragment-processor
#              (with its SQS trigger) and ship-device-gateway, plus their Function URLs
#   websocket  the ship-devices WebSocket API, its five routes, the "prod" stage
#   secrets    the three SecureString parameters under /ship/ in Parameter Store
#
# Every step checks before it creates and updates what exists, so any step can be run again.
# Not created here (see SETUP.md): Bedrock model access, the Detector in AgentCore Runtime,
# the Alexa skill (ask deploy), the Login with Amazon profile, the GitHub token, webhook and
# branch protection, and GitHub Pages.
#
# You need
#   AWS CLI v2, signed in as an IAM user or role with the policy in infra/iam/deployer-policy.json
#   (the README next to it explains what it allows); Python 3.12 or newer; the packages in
#   requirements.txt for the tables step.
#   If an administrator creates the roles, they run "./deploy.sh roles" and you run
#   "./deploy.sh --skip-roles all".
#
# Settings, from the environment or from a gitignored .env.deploy file (see the env step):
#   SHIP_ACCOUNT_ID   the only account this script touches. Set it to your own account id.
#   SHIP_REGION       default us-west-2.
#   SHIP_ENV_FILE     default .env.deploy in the repository root.
#   Values for the Lambdas:
#     GITHUB_TOKEN  GITHUB_WEBHOOK_SECRET  SHIP_DASHBOARD_TOKEN  SHIP_AGENTCORE_RUNTIME_ARN
#     SHIP_ALLOWED_REPOS  SHIP_REVIEWER_NAME  SHIP_RELAY_ALLOWED_ORIGINS  SHIP_VAPID_CONTACT
#   Values for Parameter Store:
#     SHIP_ALEXA_CLIENT_ID  SHIP_ALEXA_CLIENT_SECRET  SHIP_VAPID_PRIVATE_KEY

set -euo pipefail
umask 077
export AWS_PAGER=""

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IAM_DIR="$ROOT/infra/iam"
cd "$ROOT"

ACCOUNT_ID=${SHIP_ACCOUNT_ID:-680160265218}
REGION=${SHIP_REGION:-us-west-2}
ENV_FILE=${SHIP_ENV_FILE:-.env.deploy}

DRY_RUN=0
SKIP_ROLES=0
GEN_VAPID=0
ONLY=""
ROLLBACK_VERSION=""
STEPS=()
TMP=""

QUEUE_NAME=ship-fragment-queue
DLQ_NAME=ship-fragment-queue-dlq
QUEUE_URL=""
QUEUE_ARN=""
RELAY_ALIAS=live
API_NAME=ship-devices
STAGE_NAME=prod

# Environment variable names each Lambda receives. A leading + marks a value that must
# be provided when the function is created. On an update, a name the caller leaves unset
# keeps the value the function already has.
RELAY_ENV=(+GITHUB_TOKEN SHIP_DEVICE_GATEWAY_ENDPOINT SHIP_RELAY_ALLOWED_HOSTS SHIP_RELAY_ALLOWED_ORIGINS SHIP_DASHBOARD_URL SHIP_VAPID_CONTACT)
REVIEW_ENV=(+GITHUB_TOKEN +GITHUB_WEBHOOK_SECRET +SHIP_DASHBOARD_TOKEN +SHIP_AGENTCORE_RUNTIME_ARN
            +SHIP_FRAGMENT_QUEUE_URL SHIP_ALLOWED_REPOS SHIP_DASHBOARD_URL SHIP_DETECTOR_MODE SHIP_MODEL_BACKEND SHIP_REVIEWER_NAME)

# The pull request review path calls the AgentCore runtime, never a model in process.
export SHIP_DETECTOR_MODE=agentcore
export SHIP_MODEL_BACKEND=bedrock

NOT_FOUND_RE='ResourceNotFoundException|NotFoundException|NoSuchEntity|NonExistentQueue|QueueDoesNotExist|ParameterNotFound'
DENIED_RE='AccessDenied|not authorized|UnauthorizedOperation'

# ---------------------------------------------------------------------------------------
# Output and command helpers
# ---------------------------------------------------------------------------------------

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

dry() { [[ $DRY_RUN -eq 1 ]]; }

# Prints a command the way a shell would need it. Goes to stderr so that it never ends up
# inside a value captured with $(...). A dry run merges stderr into stdout.
show() { { printf '    $'; printf ' %q' "$@"; printf '\n'; } >&2; }

# Runs a command and hides its output. In a dry run, prints it instead.
run() { if dry; then show "$@"; else "$@" >/dev/null; fi; }

# The same, but the output is wanted (a build log, for example).
run_loud() { if dry; then show "$@"; else "$@"; fi; }

# Runs a command whose output is a value. In a dry run it prints the command and returns
# a placeholder so later commands can still be shown.
capture() {
  local placeholder=$1
  shift
  if dry; then show "$@"; printf '%s' "$placeholder"; else "$@"; fi
}

# Asks whether something exists. 0 = yes (output in PROBE_OUT), 1 = no. Any other error
# stops the script, so a permission problem is never mistaken for "missing". A dry run does
# not look at the account and reports "no", which shows the create path.
PROBE_OUT=""
probe() {
  PROBE_OUT=""
  if dry; then show "$@"; return 1; fi
  local out
  if out=$("$@" 2>&1); then PROBE_OUT=$out; return 0; fi
  if [[ $out =~ $NOT_FOUND_RE ]]; then return 1; fi
  die "$* failed: $out"
}

# Runs a create that fails with ResourceConflictException when it already exists
# (Lambda permissions, for example). That outcome counts as done.
once() {
  if dry; then show "$@"; return 0; fi
  local out
  if out=$("$@" 2>&1); then return 0; fi
  if [[ $out == *ResourceConflictException* ]]; then note "already in place"; return 0; fi
  die "$* failed: $out"
}

role_arn() { printf 'arn:aws:iam::%s:role/%s' "$ACCOUNT_ID" "$1"; }

# ---------------------------------------------------------------------------------------
# Settings and checks
# ---------------------------------------------------------------------------------------

# Reads NAME=value lines from the env file. It parses them as data and never runs the
# file. A variable the caller already set wins over the file.
load_env_file() {
  [[ -f $ENV_FILE ]] || return 0
  if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 && ! git -C "$ROOT" check-ignore -q "$ENV_FILE" 2>/dev/null; then
    warn "$ENV_FILE is not ignored by git. Add it to .gitignore before it holds a secret."
  fi
  local line name value
  local line_re='^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$'
  local dq_re='^"(.*)"$' sq_re="^'(.*)'\$"
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line%$'\r'}
    [[ $line =~ $line_re ]] || continue
    name=${BASH_REMATCH[2]}
    value=${BASH_REMATCH[3]}
    if [[ $value =~ $dq_re ]]; then value=${BASH_REMATCH[1]}; elif [[ $value =~ $sq_re ]]; then value=${BASH_REMATCH[1]}; fi
    if [[ -n ${!name:-} ]]; then continue; fi
    export "$name=$value"
  done < "$ENV_FILE"
}

check_tools() {
  if needs_aws; then command -v aws >/dev/null || die "AWS CLI not found. Install AWS CLI v2 first."; fi
  command -v python3 >/dev/null || die "python3 not found."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || die "Python 3.12 or newer is required."
}

# False when every requested step only works on local files.
needs_aws() {
  local step
  for step in "${STEPS[@]}"; do
    if [[ $step != env ]]; then return 0; fi
  done
  return 1
}

# Refuses to touch any account but the one named in SHIP_ACCOUNT_ID.
guard_account() {
  export AWS_DEFAULT_REGION=$REGION AWS_REGION=$REGION
  if dry; then
    note "dry run: no AWS call is made. A real run requires account $ACCOUNT_ID in $REGION."
    return 0
  fi
  local who have arn
  who=$(aws sts get-caller-identity --query '[Account,Arn]' --output text) \
    || die "aws sts get-caller-identity failed. Sign in with the AWS CLI first."
  have=${who%%$'\t'*}
  arn=${who#*$'\t'}
  if [[ $have != "$ACCOUNT_ID" ]]; then
    die "signed in to account $have, but this run is set for $ACCOUNT_ID. Switch credentials, or set SHIP_ACCOUNT_ID=$have to deploy there."
  fi
  note "account $have, region $REGION, as $arn"
}

# Called before a Lambda is created: every value it must have has to be present.
require_values() {
  local missing="" n
  for n in "$@"; do
    if [[ -z ${!n:-} ]]; then missing="$missing $n"; fi
  done
  if [[ -n $missing ]]; then
    if dry; then
      warn "a real run would stop here, these values are not set:$missing"
    else
      die "set these in the environment or in $ENV_FILE and run again:$missing"
    fi
  fi
}

# Writes {"Variables": {...}} for --environment to a private file and sets ENV_FILE_OUT.
# Values come from the shell variables of the same names. A name that is unset keeps the
# value the function has now. Nothing is printed.
ENV_FILE_OUT=""
env_file() {
  local fn=$1
  shift
  local names=() n
  for n in "$@"; do names+=("${n#+}"); done
  if dry; then
    local state=""
    for n in "${names[@]}"; do
      if [[ -n ${!n:-} ]]; then state="$state $n=set"; else state="$state $n=unset"; fi
    done
    note "environment for $fn:$state (values are never shown)"
    ENV_FILE_OUT="<private-env-file-for-$fn>"
    return 0
  fi
  local existing
  existing=$(aws lambda get-function-configuration --function-name "$fn" --query 'Environment.Variables' --output json 2>/dev/null) || existing=null
  ENV_FILE_OUT=$(mktemp "$TMP/env.XXXXXX")
  EXISTING_VARS=$existing python3 - "$ENV_FILE_OUT" "${names[@]}" <<'PY'
import json, os, sys
out, names = sys.argv[1], sys.argv[2:]
current = json.loads(os.environ.get("EXISTING_VARS") or "null") or {}
for name in names:
    value = os.environ.get(name)
    if value:
        current[name] = value
with open(out, "w") as fh:
    json.dump({"Variables": current}, fh)
PY
}

# 0 when the function's current value for NAME is not `value` (always in a dry run).
env_differs() {
  if dry; then return 0; fi
  local current
  current=$(aws lambda get-function-configuration --function-name "$1" --query "Environment.Variables.$2" --output text 2>/dev/null) || current=""
  [[ $current != "$3" ]]
}

# ---------------------------------------------------------------------------------------
# roles.json helpers (the list of roles is read from infra/iam/roles.json, not repeated here)
# ---------------------------------------------------------------------------------------

py_roles() {
  IAM_DIR=$IAM_DIR python3 - "$@" <<'PY'
import json, os, sys
plan = json.load(open(os.path.join(os.environ["IAM_DIR"], "roles.json")))
cmd = sys.argv[1]
if cmd == "account":
    print(plan["account"])
elif cmd == "table":
    for r in plan["roles"]:
        print("\t".join([r["role"], r["function"], r["policy"], r["file"]]))
elif cmd == "field":
    for r in plan["roles"]:
        if r["function"] == sys.argv[2]:
            print(r[sys.argv[3]])
PY
}

role_for() { py_roles field "$1" role; }

# Copies a reviewed policy to $2 with this run's account and region, and with the WebSocket
# API id named in the policy swapped for $3 (empty keeps the id as written).
render_policy() {
  local file=$1 dest=$2 api=$3
  if dry; then note "would render infra/iam/$file for account $ACCOUNT_ID into ${dest#"$ROOT"/}"; return 0; fi
  IAM_DIR=$IAM_DIR ACCOUNT_ID=$ACCOUNT_ID REGION=$REGION API_ID=$api python3 - "$IAM_DIR/$file" "$dest" <<'PY'
import json, os, re, sys
src, dest = sys.argv[1:3]
plan = json.load(open(os.path.join(os.environ["IAM_DIR"], "roles.json")))
text = open(src).read()
text = text.replace(plan["account"], os.environ["ACCOUNT_ID"]).replace(plan["region"], os.environ["REGION"])
api = os.environ.get("API_ID", "")
match = re.search(r"execute-api:[^:]+:\d+:([a-z0-9*]+)/", text)
if match and api:
    text = text.replace(":" + match.group(1) + "/", ":" + api + "/")
json.loads(text)
with open(dest, "w") as fh:
    fh.write(text)
PY
}

# The id of the ship-devices WebSocket API, or empty when it does not exist yet.
resolve_api_id() {
  if dry; then printf '<api-id>'; return 0; fi
  local id
  id=$(aws apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiId | [0]" --output text 2>/dev/null) || id=""
  if [[ $id == None || $id == null ]]; then id=""; fi
  printf '%s' "$id"
}

# 0 done, 8 not found. Anything else stops the script.
IAM_OUT=""
iam_call() {
  local out rc=0
  out=$("$@" 2>&1) || rc=$?
  if [[ $rc -eq 0 ]]; then IAM_OUT=$out; return 0; fi
  if [[ $out =~ NoSuchEntity ]]; then return 8; fi
  if [[ $out =~ $DENIED_RE ]]; then die "access denied for $1 $2 $3. Attach infra/iam/deployer-policy.json to the identity running this script."; fi
  die "$* failed: $out"
}

# ---------------------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------------------

# module, function that creates the table(s), table name(s). The modules own their key
# schemas, so this script calls them instead of repeating the schemas.
TABLE_SPECS=(
  "src.storage.alert_store create_table_if_not_exists ship-alerts"
  "src.storage.repo_store create_table_if_not_exists ship-repos"
  "src.storage.status_store create_table_if_not_exists ship-status"
  "src.storage.session_store create_table_if_not_exists ship-recent-reviews"
  "src.storage.pending_store create_table_if_not_exists ship-pending-dispositions"
  "src.storage.device_store create_table_if_not_exists ship-device-connections"
  "src.storage.push_store create_tables_if_not_exist ship-push-subscriptions,ship-pending-display"
  "src.storage.alexa_user_store create_table_if_not_exists ship-alexa-users"
  "src.rag.pattern_store create_table_if_not_exists ship-learned-patterns"
)

cmd_tables() {
  say "DynamoDB tables (each module creates its own table if it is missing)"
  if ! dry; then
    python3 -c 'import boto3, numpy' 2>/dev/null \
      || die "the local environment is missing boto3 or numpy. Run: pip install -r requirements.txt"
  fi
  local module func tables
  for spec in "${TABLE_SPECS[@]}"; do
    read -r module func tables <<<"$spec"
    note "$tables"
    run_loud env PYTHONPATH="$ROOT" python3 -c 'import importlib, sys; getattr(importlib.import_module(sys.argv[1]), sys.argv[2])()' "$module" "$func"
  done
}

# ---------------------------------------------------------------------------------------
# queues
# ---------------------------------------------------------------------------------------

cmd_queues() {
  say "SQS queues (visibility 960 s, retention 14 days, three tries before the dead-letter queue)"
  local dlq_url dlq_arn attrs="$TMP/queue-attributes.json"
  if probe aws sqs get-queue-url --queue-name "$DLQ_NAME" --query QueueUrl --output text; then
    dlq_url=$PROBE_OUT
    note "$DLQ_NAME exists"
  else
    dlq_url=$(capture '<dlq-url>' aws sqs create-queue --queue-name "$DLQ_NAME" \
      --attributes MessageRetentionPeriod=1209600 --query QueueUrl --output text)
  fi
  dlq_arn=$(capture '<dlq-arn>' aws sqs get-queue-attributes --queue-url "$dlq_url" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)

  DLQ_ARN=$dlq_arn python3 - "$attrs" <<'PY'
import json, os, sys
redrive = {"deadLetterTargetArn": os.environ["DLQ_ARN"], "maxReceiveCount": 3}
attrs = {"VisibilityTimeout": "960", "MessageRetentionPeriod": "1209600", "RedrivePolicy": json.dumps(redrive)}
with open(sys.argv[1], "w") as fh:
    json.dump(attrs, fh)
PY
  if probe aws sqs get-queue-url --queue-name "$QUEUE_NAME" --query QueueUrl --output text; then
    QUEUE_URL=$PROBE_OUT
    note "$QUEUE_NAME exists, making its settings match"
    run aws sqs set-queue-attributes --queue-url "$QUEUE_URL" --attributes "file://$attrs"
  else
    QUEUE_URL=$(capture '<queue-url>' aws sqs create-queue --queue-name "$QUEUE_NAME" \
      --attributes "file://$attrs" --query QueueUrl --output text)
  fi
  QUEUE_ARN=$(capture '<queue-arn>' aws sqs get-queue-attributes --queue-url "$QUEUE_URL" \
    --attribute-names QueueArn --query Attributes.QueueArn --output text)
  note "queue $QUEUE_URL"
}

# Sets QUEUE_URL and QUEUE_ARN for the Lambda steps when `queues` did not run in this call.
load_queue() {
  if [[ -n $QUEUE_URL && -n $QUEUE_ARN ]]; then return 0; fi
  if probe aws sqs get-queue-url --queue-name "$QUEUE_NAME" --query QueueUrl --output text; then
    QUEUE_URL=$PROBE_OUT
    QUEUE_ARN=$(aws sqs get-queue-attributes --queue-url "$QUEUE_URL" --attribute-names QueueArn --query Attributes.QueueArn --output text)
  elif dry; then
    QUEUE_URL='<queue-url>'
    QUEUE_ARN='<queue-arn>'
  else
    die "$QUEUE_NAME does not exist. Run './deploy.sh queues' first."
  fi
}

# ---------------------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------------------

cmd_roles() {
  say "IAM roles (trust policy for Lambda, plus the reviewed policy from infra/iam/ named in roles.json)"
  local out="$ROOT/build/iam" api canon
  canon=$(py_roles account)
  api=$(resolve_api_id)
  if [[ -z $api && $ACCOUNT_ID != "$canon" ]]; then
    api='*'
    note "the $API_NAME WebSocket API does not exist yet, so the policies allow any API id for now."
    note "Run './deploy.sh roles' again after 'websocket' to name this API."
  fi
  if dry; then
    note "would write build/iam/lambda-trust.json"
  else
    mkdir -p "$out"
    printf '%s\n' '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >"$out/lambda-trust.json"
  fi

  local role fn policy file
  while IFS=$'\t' read -r -u 3 role fn policy file; do
    if [[ $fn == ship-alexa-skill ]]; then continue; fi    # created by ASK, see skill-policy
    render_policy "$file" "$out/$role.policy.json" "$api"
  done 3< <(py_roles table)

  local rc try created=0
  while IFS=$'\t' read -r -u 3 role fn policy file; do
    if [[ $fn == ship-alexa-skill ]]; then continue; fi
    note "$role -> $policy"
    if dry; then
      show aws iam create-role --role-name "$role" --assume-role-policy-document "file://build/iam/lambda-trust.json"
      show aws iam put-role-policy --role-name "$role" --policy-name "$policy" --policy-document "file://build/iam/$role.policy.json"
      continue
    fi
    rc=0
    iam_call aws iam get-role --role-name "$role" || rc=$?
    if [[ $rc -eq 8 ]]; then
      rc=0
      iam_call aws iam create-role --role-name "$role" --assume-role-policy-document "file://$out/lambda-trust.json" || rc=$?
      if [[ $rc -eq 0 ]]; then created=1; fi
    fi
    if [[ $rc -eq 0 ]]; then
      # IAM is eventually consistent: a role created a moment ago can still read as missing.
      for try in 1 2 3 4 5; do
        rc=0
        iam_call aws iam put-role-policy --role-name "$role" --policy-name "$policy" --policy-document "file://$out/$role.policy.json" || rc=$?
        if [[ $rc -ne 8 ]]; then break; fi
        sleep 3
      done
    fi
    if [[ $rc -eq 8 ]]; then die "$role is not visible to IAM yet. Run './deploy.sh roles' again in a minute."; fi
  done 3< <(py_roles table)

  if [[ $created -eq 1 ]]; then
    note "waiting 10 s for new roles to become usable"
    sleep 10
  fi
}

# Attaches the reviewed policy to the role `ask deploy` created for ship-alexa-skill.
cmd_skill_policy() {
  say "Alexa skill role policy (run after 'ask deploy' has created ship-alexa-skill)"
  local arn role policy file api
  arn=$(capture "arn:aws:iam::$ACCOUNT_ID:role/<ask-lambda-role>" aws lambda get-function-configuration \
    --function-name ship-alexa-skill --query Role --output text)
  role=${arn##*/}
  policy=$(py_roles field ship-alexa-skill policy)
  file=$(py_roles field ship-alexa-skill file)
  if ! dry; then mkdir -p "$ROOT/build/iam"; fi
  render_policy "$file" "$ROOT/build/iam/ship-alexa-skill.policy.json" ""
  note "$role -> $policy"
  if dry; then
    show aws iam put-role-policy --role-name "$role" --policy-name "$policy" --policy-document "file://build/iam/ship-alexa-skill.policy.json"
    return 0
  fi
  iam_call aws iam put-role-policy --role-name "$role" --policy-name "$policy" \
    --policy-document "file://$ROOT/build/iam/ship-alexa-skill.policy.json"
}

# ---------------------------------------------------------------------------------------
# packages
# ---------------------------------------------------------------------------------------

cmd_packages() {
  say "Packages (scripts/build_packages.py builds Linux arm64 zips into build/)"
  run_loud python3 scripts/build_packages.py relay webhook fragment gateway
}

zip_path() {
  local zip="$ROOT/build/$1.zip"
  if ! dry && [[ ! -f $zip ]]; then die "$zip is missing. Run './deploy.sh packages' first."; fi
  printf '%s' "$zip"
}

# ---------------------------------------------------------------------------------------
# functions
# ---------------------------------------------------------------------------------------

selected() { [[ -z $ONLY || ",$ONLY," == *",$1,"* ]]; }

wait_updated() { run aws lambda wait function-updated-v2 --function-name "$1"; }

# aws lambda create-function, retried while a brand-new role is not yet assumable.
create_function() {
  if dry; then show aws lambda create-function "$@"; return 0; fi
  local try out
  for try in 1 2 3 4 5 6; do
    if out=$(aws lambda create-function "$@" --query FunctionArn --output text 2>&1); then return 0; fi
    if [[ $out == *"cannot be assumed"* && $try -lt 6 ]]; then
      note "the role is not usable yet, trying again in 10 s"
      sleep 10
      continue
    fi
    die "create-function failed: $out"
  done
}

# Points an alias at a version, creating the alias if it is missing.
point_alias() {
  local fn=$1 alias=$2 version=$3
  if probe aws lambda get-alias --function-name "$fn" --name "$alias" --query FunctionVersion --output text; then
    if [[ $PROBE_OUT == "$version" ]]; then
      note "alias $alias already points at version $version"
    else
      run aws lambda update-alias --function-name "$fn" --name "$alias" --function-version "$version"
    fi
  else
    run aws lambda create-alias --function-name "$fn" --name "$alias" --function-version "$version"
  fi
}

# Makes a Function URL exist with auth NONE and buffered responses, and lets the public
# invoke it (a URL with auth NONE answers 403 without that permission). Sets FUNCTION_URL.
FUNCTION_URL=""
ensure_function_url() {
  local fn=$1 qualifier=${2:-}
  local q=()
  if [[ -n $qualifier ]]; then q=(--qualifier "$qualifier"); fi
  if probe aws lambda get-function-url-config --function-name "$fn" ${q[@]+"${q[@]}"} --query FunctionUrl --output text; then
    FUNCTION_URL=$PROBE_OUT
    run aws lambda update-function-url-config --function-name "$fn" ${q[@]+"${q[@]}"} --auth-type NONE --invoke-mode BUFFERED
  else
    FUNCTION_URL=$(capture "https://<$fn-id>.lambda-url.$REGION.on.aws/" aws lambda create-function-url-config \
      --function-name "$fn" ${q[@]+"${q[@]}"} --auth-type NONE --invoke-mode BUFFERED --query FunctionUrl --output text)
  fi
  once aws lambda add-permission --function-name "$fn" ${q[@]+"${q[@]}"} \
    --statement-id FunctionURLAllowPublicAccess --action lambda:InvokeFunctionUrl \
    --principal '*' --function-url-auth-type NONE
  note "$fn URL: $FUNCTION_URL"
}

# Create or update one of the plain functions (no SnapStart): name handler arch memory timeout zip env...
deploy_lambda() {
  local fn=$1 handler=$2 arch=$3 memory=$4 timeout=$5 zip=$6
  shift 6
  local role env_args=()
  role=$(role_arn "$(role_for "$fn")")
  if [[ $# -gt 0 ]]; then
    env_file "$fn" "$@"
    env_args=(--environment "file://$ENV_FILE_OUT")
  fi
  if probe aws lambda get-function --function-name "$fn" --query Configuration.FunctionArn --output text; then
    note "$fn exists, updating its settings and code"
    run aws lambda update-function-configuration --function-name "$fn" --role "$role" --runtime python3.12 \
      --handler "$handler" --timeout "$timeout" --memory-size "$memory" ${env_args[@]+"${env_args[@]}"}
    wait_updated "$fn"
    run aws lambda update-function-code --function-name "$fn" --zip-file "fileb://$zip"
    wait_updated "$fn"
  else
    local required=() n
    for n in "$@"; do if [[ $n == +* ]]; then required+=("${n#+}"); fi; done
    if [[ ${#required[@]} -gt 0 ]]; then require_values "${required[@]}"; fi
    note "creating $fn"
    create_function --function-name "$fn" --runtime python3.12 --architectures "$arch" --role "$role" \
      --handler "$handler" --timeout "$timeout" --memory-size "$memory" --zip-file "fileb://$zip" \
      ${env_args[@]+"${env_args[@]}"}
    run aws lambda wait function-active-v2 --function-name "$fn"
  fi
}

# Publishes the current code and settings of Relay as a new version and moves the alias
# to it. SnapStart takes its snapshot when a version is published, and the version stays
# Pending until that finishes, so the alias moves only after the version is Active.
relay_release() {
  local zip role version
  zip=$(zip_path relay)
  role=$(role_arn "$(role_for ship-relay)")
  env_file ship-relay "${RELAY_ENV[@]}"
  run aws lambda update-function-configuration --function-name ship-relay --role "$role" --runtime python3.12 \
    --handler relay_lambda_handler.handler --timeout 10 --memory-size 512 \
    --snap-start ApplyOn=PublishedVersions --environment "file://$ENV_FILE_OUT"
  wait_updated ship-relay
  version=$(capture '<new-version>' aws lambda update-function-code --function-name ship-relay \
    --zip-file "fileb://$zip" --publish --query Version --output text)
  note "published version $version, waiting for the SnapStart snapshot"
  run aws lambda wait function-active-v2 --function-name ship-relay --qualifier "$version"
  point_alias ship-relay "$RELAY_ALIAS" "$version"
}

deploy_relay() {
  say "ship-relay (MCP server; SnapStart on published versions, alias $RELAY_ALIAS)"
  local zip role
  zip=$(zip_path relay)
  role=$(role_arn "$(role_for ship-relay)")
  if ! probe aws lambda get-function --function-name ship-relay --query Configuration.FunctionArn --output text; then
    require_values GITHUB_TOKEN
    env_file ship-relay "${RELAY_ENV[@]}"
    note "creating ship-relay"
    create_function --function-name ship-relay --runtime python3.12 --architectures arm64 --role "$role" \
      --handler relay_lambda_handler.handler --timeout 10 --memory-size 512 --zip-file "fileb://$zip" \
      --snap-start ApplyOn=PublishedVersions --environment "file://$ENV_FILE_OUT"
    run aws lambda wait function-active-v2 --function-name ship-relay
  fi
  relay_release

  ensure_function_url ship-relay "$RELAY_ALIAS"
  RELAY_URL=$FUNCTION_URL
  # Relay refuses any Host header it was not told about, so it learns its own URL host here.
  if [[ -z ${SHIP_RELAY_ALLOWED_HOSTS:-} ]]; then
    local host=${RELAY_URL#https://}
    host=${host%/}
    if env_differs ship-relay SHIP_RELAY_ALLOWED_HOSTS "$host"; then
      export SHIP_RELAY_ALLOWED_HOSTS=$host
      note "Relay now allows requests addressed to $host"
      relay_release
    fi
  fi
  if [[ -z ${SHIP_RELAY_ALLOWED_ORIGINS:-} ]]; then
    warn "SHIP_RELAY_ALLOWED_ORIGINS is not set. Browser pages (the simulator) cannot call Relay until it holds your Pages origin."
  fi
}

# SHIP_DASHBOARD_URL is the Gate console, on the webhook's own Function URL.
load_dashboard_url() {
  if [[ -n ${SHIP_DASHBOARD_URL:-} ]]; then return 0; fi
  if probe aws lambda get-function-url-config --function-name ship-webhook --query FunctionUrl --output text; then
    export SHIP_DASHBOARD_URL="${PROBE_OUT%/}/dashboard"
  fi
}

deploy_webhook() {
  say "ship-webhook (GitHub webhook endpoint and the Gate console)"
  load_queue
  export SHIP_FRAGMENT_QUEUE_URL=$QUEUE_URL
  local zip
  zip=$(zip_path webhook)
  deploy_lambda ship-webhook lambda_handler.handler arm64 512 900 "$zip" "${REVIEW_ENV[@]}"
  ensure_function_url ship-webhook ""
  WEBHOOK_URL=$FUNCTION_URL
  local dashboard="${WEBHOOK_URL%/}/dashboard"
  if env_differs ship-webhook SHIP_DASHBOARD_URL "$dashboard"; then
    export SHIP_DASHBOARD_URL=$dashboard
    env_file ship-webhook "${REVIEW_ENV[@]}"
    run aws lambda update-function-configuration --function-name ship-webhook --environment "file://$ENV_FILE_OUT"
    wait_updated ship-webhook
  fi
  point_relay_at_console "$dashboard"
}

# Relay hands the console's address back in its tool results, so it needs the same value as the
# webhook. Relay is deployed first, before the webhook has an address, so it is set here.
point_relay_at_console() {
  local dashboard=$1
  if probe aws lambda get-function --function-name ship-relay --query Configuration.FunctionArn --output text; then
    if env_differs ship-relay SHIP_DASHBOARD_URL "$dashboard"; then
      export SHIP_DASHBOARD_URL=$dashboard
      note "Relay now links to the Gate console"
      relay_release
    fi
  fi
}

deploy_fragment() {
  say "ship-fragment-processor (SQS consumer, one fragment per invocation)"
  load_queue
  export SHIP_FRAGMENT_QUEUE_URL=$QUEUE_URL
  load_dashboard_url
  local zip
  zip=$(zip_path fragment)
  deploy_lambda ship-fragment-processor fragment_lambda_handler.handler arm64 512 900 "$zip" "${REVIEW_ENV[@]}"

  # BatchSize 1 keeps one fragment per invocation. MaximumConcurrency 5 keeps parallel reviews
  # inside the account's Bedrock request rate.
  if probe aws lambda list-event-source-mappings --function-name ship-fragment-processor \
       --event-source-arn "$QUEUE_ARN" --query 'EventSourceMappings[0].UUID' --output text \
     && [[ $PROBE_OUT != None && -n $PROBE_OUT ]]; then
    note "SQS trigger exists, making its settings match"
    run aws lambda update-event-source-mapping --uuid "$PROBE_OUT" --batch-size 1 \
      --scaling-config MaximumConcurrency=5
  else
    run aws lambda create-event-source-mapping --function-name ship-fragment-processor \
      --event-source-arn "$QUEUE_ARN" --batch-size 1 --scaling-config MaximumConcurrency=5
  fi
}

deploy_gateway() {
  say "ship-device-gateway (WebSocket routes; no environment variables)"
  local zip
  zip=$(zip_path gateway)
  deploy_lambda ship-device-gateway device_gateway_handler.handler arm64 256 10 "$zip"
}

print_endpoints() {
  local relay="" hook=""
  if probe aws lambda get-function-url-config --function-name ship-relay --qualifier "$RELAY_ALIAS" --query FunctionUrl --output text; then
    relay=$PROBE_OUT
  fi
  if probe aws lambda get-function-url-config --function-name ship-webhook --query FunctionUrl --output text; then
    hook=$PROBE_OUT
  fi
  if dry; then return 0; fi
  say "Endpoints to copy"
  if [[ -n $hook ]]; then
    note "GitHub webhook Payload URL:  ${hook%/}/api/v1/webhook"
    note "Gate console:                ${hook%/}/dashboard"
    note "  -> SHIP_CONSOLE_URL in alexa-skill/ask-resources.json and window.SHIP_CONSOLE_URL in docs/alexa-simulator.html"
  fi
  if [[ -n $relay ]]; then
    note "Relay MCP endpoint:          ${relay%/}/mcp"
    note "  -> SHIP_RELAY_URL in alexa-skill/ask-resources.json and RELAY_URL in docs/alexa-simulator.html"
  fi
}

cmd_functions() {
  RELAY_URL=""
  WEBHOOK_URL=""
  if selected relay; then deploy_relay; fi
  if selected webhook; then deploy_webhook; fi
  if selected fragment; then deploy_fragment; fi
  if selected gateway; then deploy_gateway; fi
  print_endpoints
}

# Moves the alias back to an earlier version. With no version, lists what is published.
cmd_relay_rollback() {
  say "Relay rollback (moves alias $RELAY_ALIAS; nothing is deleted)"
  if [[ -z $ROLLBACK_VERSION ]]; then
    if dry; then show aws lambda list-versions-by-function --function-name ship-relay; return 0; fi
    note "alias $RELAY_ALIAS currently points at version $(aws lambda get-alias --function-name ship-relay --name "$RELAY_ALIAS" --query FunctionVersion --output text)"
    aws lambda list-versions-by-function --function-name ship-relay \
      --query "Versions[?Version!='\$LATEST'].[Version,LastModified,State]" --output text
    note "Run './deploy.sh relay-rollback VERSION' to move the alias."
    return 0
  fi
  run aws lambda wait function-active-v2 --function-name ship-relay --qualifier "$ROLLBACK_VERSION"
  point_alias ship-relay "$RELAY_ALIAS" "$ROLLBACK_VERSION"
}

# ---------------------------------------------------------------------------------------
# websocket
# ---------------------------------------------------------------------------------------

cmd_websocket() {
  say "WebSocket API $API_NAME (routes \$connect, \$disconnect, register, subscribe, ack -> ship-device-gateway)"
  local api fn_arn uri integ routes key
  if ! probe aws lambda get-function --function-name ship-device-gateway --query Configuration.FunctionArn --output text && ! dry; then
    die "ship-device-gateway does not exist. Run './deploy.sh functions' first."
  fi
  fn_arn=$(capture "<gateway-function-arn>" aws lambda get-function-configuration \
    --function-name ship-device-gateway --query FunctionArn --output text)

  api=""
  if ! dry; then api=$(resolve_api_id); fi
  if [[ -z $api ]]; then
    api=$(capture '<api-id>' aws apigatewayv2 create-api --name "$API_NAME" --protocol-type WEBSOCKET \
      --route-selection-expression '$request.body.action' --query ApiId --output text)
    NEW_API=1
  else
    note "$API_NAME exists ($api)"
  fi

  uri="arn:aws:apigateway:$REGION:lambda:path/2015-03-31/functions/$fn_arn/invocations"
  integ=""
  if ! dry; then
    integ=$(aws apigatewayv2 get-integrations --api-id "$api" --query "Items[?IntegrationUri=='$uri'].IntegrationId | [0]" --output text)
    if [[ $integ == None ]]; then integ=""; fi
  fi
  if [[ -z $integ ]]; then
    integ=$(capture '<integration-id>' aws apigatewayv2 create-integration --api-id "$api" \
      --integration-type AWS_PROXY --integration-uri "$uri" --query IntegrationId --output text)
  fi

  routes=""
  if ! dry; then routes=$(aws apigatewayv2 get-routes --api-id "$api" --query 'Items[].RouteKey' --output text | tr '\t' '\n'); fi
  # shellcheck disable=SC2016  # the dollar signs are part of the route names
  for key in '$connect' '$disconnect' register subscribe ack; do
    if grep -Fxq -- "$key" <<<"$routes"; then
      note "route $key exists"
    else
      run aws apigatewayv2 create-route --api-id "$api" --route-key "$key" --target "integrations/$integ"
    fi
  done

  # The stage has no auto-deploy, so every route change needs a new deployment to go live.
  local deployment
  if probe aws apigatewayv2 get-stage --api-id "$api" --stage-name "$STAGE_NAME" --query StageName --output text; then
    run aws apigatewayv2 create-deployment --api-id "$api" --stage-name "$STAGE_NAME"
  else
    deployment=$(capture '<deployment-id>' aws apigatewayv2 create-deployment --api-id "$api" --query DeploymentId --output text)
    run aws apigatewayv2 create-stage --api-id "$api" --stage-name "$STAGE_NAME" --deployment-id "$deployment"
  fi

  once aws lambda add-permission --function-name ship-device-gateway --statement-id "apigw-$api" \
    --action lambda:InvokeFunction --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT_ID:$api/*/*"

  local ws="wss://$api.execute-api.$REGION.amazonaws.com/$STAGE_NAME"
  local mgmt="https://$api.execute-api.$REGION.amazonaws.com/$STAGE_NAME"
  note "WebSocket URL (WS_URL in docs/ship-display.html): $ws"
  note "Management endpoint (SHIP_DEVICE_GATEWAY_ENDPOINT):  $mgmt"

  # Relay pushes to open pages through the management endpoint, which exists only now.
  export SHIP_DEVICE_GATEWAY_ENDPOINT=$mgmt
  if probe aws lambda get-function --function-name ship-relay --query Configuration.FunctionArn --output text || dry; then
    if env_differs ship-relay SHIP_DEVICE_GATEWAY_ENDPOINT "$mgmt"; then
      note "giving Relay the management endpoint (new version, alias moves)"
      relay_release
    fi
  else
    warn "ship-relay does not exist yet. Run './deploy.sh functions' so Relay learns the management endpoint."
  fi
  if [[ ${NEW_API:-0} -eq 1 ]]; then
    note "The roles were written before this API existed. Run './deploy.sh roles' once more so they name API $api."
  fi
}

# ---------------------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------------------

# parameter name, the variable that can supply its value
SSM_PARAMS=(
  "/ship/alexa/client_id SHIP_ALEXA_CLIENT_ID"
  "/ship/alexa/client_secret SHIP_ALEXA_CLIENT_SECRET"
  "/ship/webpush/vapid_private SHIP_VAPID_PRIVATE_KEY"
)

# Makes a VAPID key pair. The private half goes to SHIP_VAPID_PRIVATE_KEY and is never
# shown. Only the public half is printed.
generate_vapid() {
  if probe aws ssm get-parameter --name /ship/webpush/vapid_private --query Parameter.Name --output text; then
    die "/ship/webpush/vapid_private already exists. A new key would stop every registered screen from receiving notifications. Replace it by hand if that is what you want."
  fi
  if dry; then
    note "would generate a key pair with py-vapid; the private key goes into the parameter, the public key is printed"
    return 0
  fi
  python3 -c 'import py_vapid, cryptography' 2>/dev/null || die "py-vapid is missing. Run: pip install py-vapid"
  local keyfile="$TMP/vapid-private" public
  public=$(python3 - "$keyfile" <<'PY'
import sys
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02, b64urlencode

vapid = Vapid02()
vapid.generate_keys()
private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
public = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
with open(sys.argv[1], "w") as fh:
    fh.write(b64urlencode(private))
print(b64urlencode(public))
PY
)
  SHIP_VAPID_PRIVATE_KEY=$(<"$keyfile")
  export SHIP_VAPID_PRIVATE_KEY
  note "public key for VAPID_PUBLIC_KEY in docs/ship-display.html:"
  note "$public"
}

cmd_secrets() {
  say "Parameter Store (SecureString, default key). Values are never printed."
  if [[ $GEN_VAPID -eq 1 ]]; then generate_vapid; fi
  local entry name var value exists missing=()
  for entry in "${SSM_PARAMS[@]}"; do
    read -r name var <<<"$entry"
    value=${!var:-}
    exists=0
    if probe aws ssm get-parameter --name "$name" --query Parameter.Name --output text; then exists=1; fi
    if [[ -z $value && $exists -eq 0 && -t 0 ]] && ! dry; then
      read -rsp "$name (input hidden): " value
      printf '\n'
    fi
    if [[ -z $value ]]; then
      if [[ $exists -eq 1 ]]; then
        note "$name exists, left unchanged (set $var to replace it)"
      elif dry; then
        note "$name: $var is not set; a real run would ask for it"
      else
        missing+=("$name ($var)")
      fi
      continue
    fi
    export "$var=$value"
    if dry; then
      show aws ssm put-parameter --cli-input-json "file://<private-file-holding-$var>"
      continue
    fi
    local request="$TMP/ssm-request.json"
    PARAM_NAME=$name python3 - "$request" "$var" <<'PY'
import json, os, sys
request, var = sys.argv[1:3]
with open(request, "w") as fh:
    json.dump({"Name": os.environ["PARAM_NAME"], "Value": os.environ[var], "Type": "SecureString", "Overwrite": True}, fh)
PY
    run aws ssm put-parameter --cli-input-json "file://$request"
    rm -f -- "$request"
    note "$name stored"
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    warn "not stored yet: ${missing[*]}"
    note "Provide each one through the variable in parentheses, or run this in a terminal to be asked."
    note "Alexa client id and secret come from the Login with Amazon security profile (SETUP.md)."
    note "For the VAPID key run: ./deploy.sh secrets --generate-vapid"
  fi
}

# ---------------------------------------------------------------------------------------
# env: a starter env file that is never printed
# ---------------------------------------------------------------------------------------

cmd_env() {
  say "Environment file $ENV_FILE"
  if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 && ! git -C "$ROOT" check-ignore -q "$ENV_FILE" 2>/dev/null; then
    die "$ENV_FILE is not ignored by git. Add the line '$ENV_FILE' to .gitignore first, then run this again."
  fi
  if dry; then
    note "would create $ENV_FILE (mode 600) with empty slots, and random values for GITHUB_WEBHOOK_SECRET and SHIP_DASHBOARD_TOKEN if absent"
    return 0
  fi
  if [[ ! -f $ENV_FILE ]]; then
    cat >"$ENV_FILE" <<'EOF'
# Read by deploy.sh as NAME=value lines. One value per line, no quotes needed, no trailing comments.
# Fill these in. Keep this file out of git.
GITHUB_TOKEN=
SHIP_AGENTCORE_RUNTIME_ARN=
SHIP_ALLOWED_REPOS=
SHIP_REVIEWER_NAME=
SHIP_RELAY_ALLOWED_ORIGINS=
SHIP_VAPID_CONTACT=
SHIP_ALEXA_CLIENT_ID=
SHIP_ALEXA_CLIENT_SECRET=
EOF
  fi
  chmod 600 "$ENV_FILE"
  local name
  for name in GITHUB_WEBHOOK_SECRET SHIP_DASHBOARD_TOKEN; do
    if grep -Eq "^[[:space:]]*(export[[:space:]]+)?$name=." "$ENV_FILE"; then
      note "$name already has a value"
    else
      printf '%s=%s\n' "$name" "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" >>"$ENV_FILE"
      note "$name written to $ENV_FILE (not shown)"
    fi
  done
  note "Open $ENV_FILE and fill in the rest."
}

# ---------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------

usage() {
  cat <<'EOF'
Usage: ./deploy.sh [options] STEP [STEP ...]

Steps (each can be run again):
  env             create a gitignored .env.deploy with random webhook and dashboard secrets
  tables          the ten DynamoDB tables
  queues          ship-fragment-queue and its dead-letter queue
  roles           the four Lambda roles and their policies
  packages        build the four zips with scripts/build_packages.py
  functions       the four Lambdas, Function URLs, Relay alias and SQS trigger
  websocket       the ship-devices WebSocket API, routes, stage and permission
  secrets         the three SSM SecureString parameters
  skill-policy    attach the reviewed policy to the Alexa skill's role (after ask deploy)
  relay-rollback [VERSION]
                  move Relay's alias "live" to VERSION; with no VERSION, list versions
  all             tables queues roles packages functions websocket secrets, in that order

Options:
  --dry-run          print every command and run none. Makes no AWS call.
  --only a,b         with functions: only relay, webhook, fragment or gateway
  --skip-roles       leave out the roles step of "all"
  --generate-vapid   with secrets: make a VAPID key pair and store the private half
  -h, --help         this text

Settings: SHIP_ACCOUNT_ID, SHIP_REGION (default us-west-2), SHIP_ENV_FILE (default .env.deploy).
Permissions needed to run it: infra/iam/deployer-policy.json
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      -h|--help) usage; exit 0 ;;
      --dry-run) DRY_RUN=1 ;;
      --skip-roles) SKIP_ROLES=1 ;;
      --generate-vapid) GEN_VAPID=1 ;;
      --only) shift; [[ $# -gt 0 ]] || die "--only needs a list, for example --only relay,webhook"; ONLY=$1 ;;
      --only=*) ONLY=${1#--only=} ;;
      -*) usage >&2; die "unknown option $1" ;;
      [0-9]*) ROLLBACK_VERSION=$1 ;;
      *) STEPS+=("$1") ;;
    esac
    shift
  done
}

expand_steps() {
  local step expanded=()
  for step in "${STEPS[@]}"; do
    if [[ $step == all ]]; then
      expanded+=(tables queues)
      if [[ $SKIP_ROLES -eq 0 ]]; then expanded+=(roles); fi
      expanded+=(packages functions websocket secrets)
    else
      expanded+=("$step")
    fi
  done
  STEPS=("${expanded[@]}")
}

main() {
  parse_args "$@"
  if [[ ${#STEPS[@]} -eq 0 ]]; then usage >&2; exit 2; fi
  expand_steps
  local step
  for step in "${STEPS[@]}"; do
    case $step in
      env|tables|queues|roles|packages|functions|websocket|secrets|skill-policy|relay-rollback) ;;
      *) usage >&2; die "unknown step '$step'" ;;
    esac
  done

  if dry; then exec 2>&1; fi
  TMP=$(mktemp -d)
  trap 'rm -r -- "$TMP"' EXIT

  check_tools
  load_env_file
  if needs_aws; then
    say "SHIP deploy: account $ACCOUNT_ID, region $REGION$(if dry; then printf ' (dry run)'; fi)"
    if dry; then note "A dry run does not look at your account, so it shows the create path for every resource."; fi
    guard_account
  fi

  for step in "${STEPS[@]}"; do
    case $step in
      env) cmd_env ;;
      tables) cmd_tables ;;
      queues) cmd_queues ;;
      roles) cmd_roles ;;
      packages) cmd_packages ;;
      functions) cmd_functions ;;
      websocket) cmd_websocket ;;
      secrets) cmd_secrets ;;
      skill-policy) cmd_skill_policy ;;
      relay-rollback) cmd_relay_rollback ;;
    esac
  done
  say "Done"
}

main "$@"
