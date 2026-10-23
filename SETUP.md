# Setting up SHIP

This guide takes an empty AWS account to a working SHIP deployment. Most of the work is done by [`deploy.sh`](deploy.sh). A few steps need a console or a browser. They are marked as console steps.

SHIP runs in one AWS account and one region. The reference deployment uses account `680160265218` and `us-west-2`. The repository still holds that account id, and a few other values from it, in some files. Section 3 lists them.

## 1. Scope of this proof of concept

The webhook checks an HMAC signature on every GitHub delivery. Gate, the review console, asks for a dashboard token. Relay and the WebSocket API do not authenticate callers in code.

Relay accepts only the Host and Origin values it is configured with. That stops a page on another site from calling it in a browser. It does not stop a direct call. Anyone who holds the Relay or WebSocket URL can call Relay's tools, the decision tools included, and can register a screen name. Both URLs end up in the public display and simulator pages. The decision rules (a finding must be shown on a screen, a reason is required, the person confirms) are procedural checks, not an identity check. An authorizer in front of both endpoints and a verified identity on each decision are not built.

## 2. What you need

### Accounts

- An AWS account, with an identity for the deploy steps (below) and an administrator identity for IAM.
- A GitHub account that owns the repository SHIP will review (the demo repository), and a copy of this repository to host the display pages.
- An Amazon developer account, for the Alexa skill and Login with Amazon.
- An Echo device, to hear the guided review and the alert chime.

The identity that runs `deploy.sh` needs the policy in [`infra/iam/deployer-policy.json`](infra/iam/deployer-policy.json). [`infra/iam/README.md`](infra/iam/README.md) says what it allows and how to attach it. If an administrator creates the roles, they run `./deploy.sh roles`, and you run `./deploy.sh --skip-roles all`.

### Tools

| Tool | Used for | Check |
|---|---|---|
| AWS CLI v2 | everything in `deploy.sh` | `aws --version` |
| Python 3.12 or newer, pip | `deploy.sh`, `scripts/` | `python3 --version` |
| Node.js 20 or newer, npm | AgentCore CLI | `node --version` |
| uv | AgentCore Python build | `uv --version` |
| AgentCore CLI | deploys Detector | `agentcore --help` |
| ASK CLI | deploys the Alexa skill | `ask --version` |
| GitHub CLI (`gh`) | `scripts/preflight.py`, `scripts/demo_reset.py` | `gh auth status` |

### Console steps that cannot be scripted

| Step | Where | When |
|---|---|---|
| Enable Bedrock model access for Amazon Nova Lite and Titan embeddings in `us-west-2`. A new account can also need a use-case form or a quota request before calls succeed. | Bedrock console, Model access | Before section 4.2 |
| Create a GitHub token that can read pull requests and write commit statuses and issue/PR comments. | GitHub, Settings, Developer settings | Before section 4.9 |
| Create the Login with Amazon security profile of the skill and copy its client id and client secret. SHIP uses them to send Echo alerts through the Alexa proactive events API. The skill does not use account linking. | Amazon developer console | After `ask deploy`, section 5 |
| Add the GitHub webhook. | Repository Settings, Webhooks | After section 4.9 |
| Turn on GitHub Pages for the `docs/` folder. | Repository Settings, Pages | Section 4.13 |
| Make `ship/compliance` a required status check. | Repository Settings, Branches | After the first run, section 9 |

## 3. Change the values that name the reference deployment

Edit these in your copy before you build. The URLs marked "later" exist only after section 4.9 or 4.10, so come back to them.

| File | Value | New value |
|---|---|---|
| `shipagentcore/agentcore/aws-targets.json` | `account` | your account id |
| `alexa-skill/skill-package/skill.json` | the account id in three `ship-alexa-skill` Lambda ARNs | your account id |
| `src/relay/webpush.py` | `DISPLAY_PAGE`, the page a notification opens | your Pages URL for `ship-display.html` |
| `alexa-skill/ask-resources.json` | `SHIP_RELAY_URL`, `SHIP_CONSOLE_URL` (later) | Relay URL plus `mcp`, Gate URL plus `dashboard` |
| `docs/ship-display.html` | `WS_URL` (later), `VAPID_PUBLIC_KEY` (later) | your WebSocket URL, your public VAPID key |
| `docs/alexa-simulator.html` | `RELAY_URL`, `window.SHIP_CONSOLE_URL` (later) | same URLs as `ask-resources.json` |
| `scripts/preflight.py` | `ACCOUNT`, `WS_URL`, `SKILL_ID`, `PAGES_BASE`, and `--repo` | your values |
| `scripts/demo_reset.py` | `REPO`, `OWNER`, `DEMOS` | your demo repository, its owner, its demo branches |

`src/relay/webpush.py` is packaged into Relay, so edit it before `./deploy.sh packages`.

The policy files in `infra/iam/` name the reference account and WebSocket API. `deploy.sh roles` fills in your account and API id as it writes each role's policy. Do not edit those files for that.

## 4. Build order

| Step | Command | Creates |
|---|---|---|
| 4.1 | local setup | virtual environment |
| 4.2 | Bedrock check | nothing |
| 4.3 | `agentcore deploy` | Detector runtime |
| 4.4 | `./deploy.sh env` | `.env.deploy` |
| 4.5 | `./deploy.sh --dry-run all` | nothing |
| 4.6 | `./deploy.sh tables queues` | 10 tables, 2 queues |
| 4.7 | `./deploy.sh roles` | 4 roles |
| 4.8 | `./deploy.sh packages` | 4 zips |
| 4.9 | `./deploy.sh functions` | 4 functions, 2 Function URLs, SQS trigger |
| 4.10 | `./deploy.sh websocket` | WebSocket API |
| 4.11 | `./deploy.sh roles` | narrows two policies to the API |
| 4.12 | `./deploy.sh secrets --generate-vapid` | VAPID parameter |
| 4.13 | Pages | display pages |

`./deploy.sh all` runs the `deploy.sh` steps in this order: tables, queues, roles, packages, functions, websocket, secrets. Detector (4.3) and your settings (4.4) have to be in place before `functions`, so the numbered path below is the safer first run. Every `deploy.sh` step can be run again. It checks first and updates what exists. It never deletes anything.

### 4.1 Local setup

```bash
git clone https://github.com/<owner>/ship-engine.git
cd ship-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export SHIP_ACCOUNT_ID=<your 12-digit account id>
aws sts get-caller-identity
```

Check that the `Account` in the last output equals `SHIP_ACCOUNT_ID`. `deploy.sh` refuses to run in any other account. `SHIP_REGION` defaults to `us-west-2`, the only region the reference deployment uses.

### 4.2 Check that Bedrock works

```bash
SHIP_MODEL_BACKEND=bedrock SHIP_DETECTOR_MODE=in_process python3 -m src.agents.detector
```

Check that it prints a structured verdict for a sample violation. An access or quota error means model access is not enabled yet. See the console steps in section 2.

### 4.3 Deploy Detector to AgentCore Runtime

Edit `shipagentcore/agentcore/aws-targets.json` first (section 3).

```bash
npm install -g @aws/agentcore
cd shipagentcore
agentcore deploy --yes
cd ..
```

The output names the runtime ARN. Keep it. It becomes `SHIP_AGENTCORE_RUNTIME_ARN` in the next step.

### 4.4 Write your settings

`deploy.sh` reads settings from your environment or from `.env.deploy`. It parses that file as `NAME=value` lines and does not run it. It never prints a value.

`.env.deploy` is listed in `.gitignore`, so it never reaches the repository.

```bash
./deploy.sh env
```

The `env` step writes `.env.deploy` with mode 600. It fills `GITHUB_WEBHOOK_SECRET` and `SHIP_DASHBOARD_TOKEN` with random values and leaves the rest empty. Open the file and fill in the others.

| Name | Used by | Value |
|---|---|---|
| `GITHUB_TOKEN` | webhook, fragment processor, Relay | the token from section 2 |
| `GITHUB_WEBHOOK_SECRET` | webhook, fragment processor | random. The same value goes into the GitHub webhook. |
| `SHIP_DASHBOARD_TOKEN` | webhook, fragment processor | random. This is the Gate login. Keep it different from the webhook secret. |
| `SHIP_AGENTCORE_RUNTIME_ARN` | webhook, fragment processor | the ARN from 4.3 |
| `SHIP_ALLOWED_REPOS` | webhook, fragment processor | optional, `<owner>/<repo>`. Pre-seeds the allowlist. |
| `SHIP_REVIEWER_NAME` | webhook, fragment processor | optional, the name shown on decisions |
| `SHIP_RELAY_ALLOWED_ORIGINS` | Relay | your Pages origin, for example `https://<owner>.github.io` |
| `SHIP_VAPID_CONTACT` | Relay | optional, `mailto:you@example.com`. Without it, Relay sends the project owner's address. |
| `SHIP_ALEXA_CLIENT_ID`, `SHIP_ALEXA_CLIENT_SECRET` | `secrets` step | from section 5 |
| `SHIP_VAPID_PRIVATE_KEY` | `secrets` step | made by `--generate-vapid` |

`deploy.sh` sets the rest itself. `SHIP_DETECTOR_MODE` is `agentcore` and `SHIP_MODEL_BACKEND` is `bedrock`. The queue URL, the Gate URL, the Relay host and the WebSocket management endpoint are read from what it creates. The Gate URL goes to the webhook, the fragment processor and Relay, so Relay's tool results link to the console.

### 4.5 Read the plan

```bash
./deploy.sh --dry-run all
```

A dry run prints every command and runs none. It makes no AWS call, so it shows the create path for every resource. It also lists any required value that is still unset.

### 4.6 Tables and queues

```bash
./deploy.sh tables queues
```

`tables` calls the create function in each storage module, so each key schema lives in one place. All ten tables use on-demand billing. `queues` creates `ship-fragment-queue` (visibility timeout 960 s, retention 14 days, redrive to the dead-letter queue after 3 receives) and `ship-fragment-queue-dlq`.

| Table | Created by |
|---|---|
| `ship-alerts` | `src.storage.alert_store.create_table_if_not_exists` |
| `ship-repos` | `src.storage.repo_store.create_table_if_not_exists` |
| `ship-status` | `src.storage.status_store.create_table_if_not_exists` |
| `ship-recent-reviews` | `src.storage.session_store.create_table_if_not_exists` |
| `ship-pending-dispositions` | `src.storage.pending_store.create_table_if_not_exists` |
| `ship-device-connections` | `src.storage.device_store.create_table_if_not_exists` |
| `ship-push-subscriptions`, `ship-pending-display` | `src.storage.push_store.create_tables_if_not_exist` |
| `ship-alexa-users` | `src.storage.alexa_user_store.create_table_if_not_exists` |
| `ship-learned-patterns` | `src.rag.pattern_store.create_table_if_not_exists` |

Only `alert_store`, `alexa_user_store` and `push_store` can be run with `python3 -m`. Running `alert_store` that way also writes and resolves a sample finding, so use `deploy.sh`.

Confirm it worked:

```bash
aws dynamodb list-tables --query TableNames --output text
aws sqs list-queues --queue-name-prefix ship-fragment --output text
```

### 4.7 Roles

```bash
./deploy.sh roles
```

This creates the four Lambda roles with a Lambda trust policy and gives each one policy from `infra/iam/`, named as in `infra/iam/roles.json`.

| Role | Policy | Function |
|---|---|---|
| `ship-relay-role` | `ship-relay-access` | `ship-relay` |
| `ship-device-gateway-role` | `ship-device-gateway-access` | `ship-device-gateway` |
| `ship-fragment-processor-role` | `ship-fragment-processor-access` | `ship-fragment-processor` |
| `ship-webhook-AgentCore-lambda-role` | `ship-webhook-access` | `ship-webhook` |

The Alexa skill's role is made by `ask deploy` (section 5).

In a new account the WebSocket API does not exist yet, so the Relay and gateway policies allow any API id. Run `./deploy.sh roles` again after step 4.11 to name yours.

Check, with an identity that can read IAM:

```bash
aws iam get-role-policy --role-name ship-relay-role --policy-name ship-relay-access --query PolicyName
```

### 4.8 Packages

Edit `src/relay/webpush.py` first (section 3).

```bash
./deploy.sh packages
ls -lh build/*.zip
```

This calls `scripts/build_packages.py`. It builds `relay`, `webhook`, `fragment` and `gateway` zips for Linux arm64 and Python 3.12, whatever machine you build on. Each zip holds the dependencies and the first-party files its handler imports. The Alexa skill is not built here.

### 4.9 Functions

Check that `GITHUB_TOKEN` and `SHIP_AGENTCORE_RUNTIME_ARN` are set, then run:

```bash
./deploy.sh functions
```

| Function | Handler | Arch | Memory | Timeout | Notes |
|---|---|---|---|---|---|
| `ship-relay` | `relay_lambda_handler.handler` | arm64 | 512 MB | 10 s | SnapStart on published versions, alias `live`, Function URL on the alias |
| `ship-webhook` | `lambda_handler.handler` | arm64 | 512 MB | 900 s | Function URL. Serves the GitHub webhook and Gate at `/dashboard`. |
| `ship-fragment-processor` | `fragment_lambda_handler.handler` | arm64 | 512 MB | 900 s | SQS trigger on `ship-fragment-queue`, batch size 1, maximum concurrency 5 |
| `ship-device-gateway` | `device_gateway_handler.handler` | arm64 | 256 MB | 10 s | no environment variables |

All four use Python 3.12. Both Function URLs use auth type NONE and buffered responses. The script also adds the public invoke permission a URL with no auth needs. CORS is handled in code.

SnapStart needs a published version, so Relay is deployed in a fixed order. The script updates the configuration, uploads the code with `--publish`, waits until that version is Active, then moves the alias `live` to it. Relay's own Function URL host goes into `SHIP_RELAY_ALLOWED_HOSTS`, so Relay publishes a second version on a first run.

At the end the script prints the values to copy. These are the GitHub webhook Payload URL, the Gate URL and the Relay MCP URL.

Confirm with:

```bash
aws lambda list-functions --query "Functions[?starts_with(FunctionName,'ship-')].[FunctionName,Runtime,Architectures[0]]" --output table
aws lambda get-alias --function-name ship-relay --name live --query FunctionVersion
curl "$(aws lambda get-function-url-config --function-name ship-webhook --query FunctionUrl --output text)health"
```

The last command should print `{"status":"ok"}`.

### 4.10 WebSocket API

```bash
./deploy.sh websocket
```

This creates the `ship-devices` WebSocket API with route selection `$request.body.action` and the routes `$connect`, `$disconnect`, `register`, `subscribe` and `ack`. All five use one Lambda proxy integration with `ship-device-gateway`. It then deploys stage `prod` and lets API Gateway invoke the function. The stage has no auto-deploy, so every run ends with a new deployment.

The script prints two addresses.

- The WebSocket URL, `wss://<api-id>.execute-api.us-west-2.amazonaws.com/prod`, goes into `WS_URL` in `docs/ship-display.html`.
- The management endpoint, `https://<api-id>.execute-api.us-west-2.amazonaws.com/prod`, goes into Relay's `SHIP_DEVICE_GATEWAY_ENDPOINT`. The script sets that itself and publishes a new Relay version.

Look at the result:

```bash
aws apigatewayv2 get-apis --query "Items[?Name=='ship-devices'].[ApiId,RouteSelectionExpression]" --output text
aws apigatewayv2 get-routes --api-id <api-id> --query 'Items[].RouteKey' --output text
```

### 4.11 Name the API in the roles

```bash
./deploy.sh roles
```

Run it again now that the API has an id. Relay and the gateway may then call only that API's connections.

### 4.12 VAPID key pair

Web Push needs a key pair. The private half is read by Relay from Parameter Store. The public half goes into the display page.

```bash
pip install py-vapid
./deploy.sh secrets --generate-vapid
```

The command stores the private key as the SecureString `/ship/webpush/vapid_private`, with the default key. It prints only the public key. Copy that into `VAPID_PUBLIC_KEY` in `docs/ship-display.html`.

The parameter holds the key as URL-safe base64, 43 characters, not as PEM text. Relay hands the string to `pywebpush`, which reads a base64 key. If you make a pair by hand with py-vapid or pywebpush, store that form. The step refuses to replace an existing key, because a new key stops every registered screen from receiving notifications.

Confirm that the parameters exist:

```bash
aws ssm get-parameters-by-path --path /ship --recursive --query 'Parameters[].Name' --output text
```

### 4.13 Pages

Edit `WS_URL`, `VAPID_PUBLIC_KEY` and the simulator constants (section 3). Commit and push. In the repository, open Settings, Pages, and publish from the branch and the `docs/` folder. Wait for the publish to finish.

Check that `https://<owner>.github.io/<repo>/ship-display.html` loads and its page source shows your `WS_URL`.

### 4.14 GitHub webhook

In the demo repository, open Settings, Webhooks, Add webhook.

| Field | Value |
|---|---|
| Payload URL | the webhook Function URL plus `api/v1/webhook` |
| Content type | `application/json` |
| Secret | the value of `GITHUB_WEBHOOK_SECRET` |
| Events | Pull requests |

The webhook route is `/api/v1/webhook`. A delivery to the bare Function URL does not reach it.

## 5. The Alexa skill

The skill lives in `alexa-skill/` (`skill.json`, the en-US interaction model and the Lambda). It needs the notifications permission and the proactive events API, both declared in `skill.json`.

Put your account id into `skill.json` and your Relay and Gate URLs into `ask-resources.json` (section 3). Then:

```bash
cd alexa-skill
ask configure
ask deploy
cd ..
```

`ask deploy` builds the interaction model, creates the Lambda `ship-alexa-skill` (512 MB, 8 s, with `SHIP_RELAY_URL`, `SHIP_CONSOLE_URL` and `SHIP_ALEXA_USERS_TABLE`) and creates its role, named `ask-lambda-SHIPReview-<number>`. A new account gets its own skill id. Find it in `alexa-skill/.ask/ask-states.json` under `skillId`, and put it into `SKILL_ID` in `scripts/preflight.py`.

Give the role its reviewed policy:

```bash
./deploy.sh skill-policy
```

That writes `ship-alexa-skill-access` to the role. It allows logs, one `PutItem` on `ship-alexa-users`, and `bedrock:InvokeModel` on Amazon Nova Lite. Like `roles`, it hands over the command when you lack IAM rights.

Store the Login with Amazon credentials of the skill (section 2) as `SHIP_ALEXA_CLIENT_ID` and `SHIP_ALEXA_CLIENT_SECRET` in `.env.deploy`, then:

```bash
./deploy.sh secrets
```

The step stores `/ship/alexa/client_id` and `/ship/alexa/client_secret`. It leaves existing values alone unless you set the variable. If you run it in a terminal with a value missing, it asks for the value with hidden input.

Turn on Echo alerts by saying "Alexa, ask ship review to turn on alerts". Alexa sends a permission request to your Alexa app. Allow it there. The first time you open the skill, the Lambda saves your user in `ship-alexa-users`, and from then on SHIP sends one notification per new finding to that user.

Check:

```bash
ask smapi get-skill-status -s <skill-id>
aws dynamodb scan --table-name ship-alexa-users --select COUNT
```

The first should show `SUCCEEDED`. The count is at least 1 after you have opened the skill once.

## 6. Register screens

A screen is any browser.

1. Open `https://<owner>.github.io/<repo>/ship-display.html` on the device.
2. Choose a name, such as `laptop` or `tablet`.
3. Tap "Get alerts on this screen".
4. Allow notifications once.

There is no pairing code. On an iPhone or iPad, add the page to the Home Screen first. An open page sits idle until Relay pushes to it. A closed page that registered for alerts gets a Web Push notification, and tapping it opens the page.

Check:

```bash
aws dynamodb scan --table-name ship-push-subscriptions --select COUNT
python scripts/preflight.py --screens laptop,tablet --repo <owner>/<repo>
```

The first count is at least 1. In the preflight output, "Screens registered" lists the names.

## 7. Connect a repository

Open the Gate console once with the dashboard token:

```
https://<webhook-function-url>/dashboard/repos?token=<SHIP_DASHBOARD_TOKEN>
```

Connect `<owner>/<repo>`. The token in the link sets a session cookie, and later pages are plain links. A cold visit to `/dashboard` shows a login form instead. A delivery that names a repository you have not connected is rejected before it can spend your GitHub token or model quota. `SHIP_ALLOWED_REPOS` pre-seeds the same list.

Check that the repository shows as "awaiting first event" in the connected repositories view. It changes to "watching" after the first delivery.

## 8. Verify

```bash
python scripts/preflight.py --screens laptop --repo <owner>/<repo>
```

Preflight is read-only. It prints PASS, WARN or FAIL for the account, the five Lambdas, the Relay alias, Relay's MCP tools, the WebSocket gateway, registered screens, demo findings and their checks, the dead-letter queue, recent errors in the logs, the skill build, the alert credentials and the public pages. It exits with status 1 if anything fails. Right after a first setup, expect warnings that no demo findings exist yet. Fix every FAIL before you continue.

## 9. Run a pull request through it

Open a pull request on the connected repository that does something SHIP flags. The easiest is raw personal data reaching an external model call, for example a prompt built from an applicant's date of birth and income. The demo repository branches `feature/ai-underwriting-assistant` and `feature/ai-admin-and-risk-tools` hold such changes. `scripts/demo_reset.py` opens pull requests from them.

You should see:

1. The `ship/compliance` status on the pull request turns `pending`, then `failure` with "1 finding needs a decision". A pull request with nothing to flag turns `success`.
2. The finding appears in Gate, and the pull request gets a comment.
3. Your Echo chimes if alerts are on. Say "Alexa, ask ship review what's up".

Then make `ship/compliance` a required status check in the repository's branch protection. Without that rule the status shows on the pull request but does not stop the merge.

## 10. Reset the demo

Between takes, or when the demo data is spent:

```bash
python scripts/demo_reset.py          # shows the plan
python scripts/demo_reset.py --yes    # does it
python scripts/preflight.py
```

The reset closes the open demo pull requests, deletes their branches, deletes every finding in `ship-alerts`, opens one new pull request per demo change and waits for SHIP to review them (420 s by default, `--wait` changes it). It needs `gh` signed in as the account that owns the demo repository. It changes GitHub and DynamoDB, so without `--yes` it only prints the plan.

## 11. Update a deployment

Each zip carries its own copy of the shared code under `src/`. A change to shared code reaches a function only when that function is rebuilt and deployed. Deploy in this order:

```bash
./deploy.sh packages
./deploy.sh functions          # relay, webhook, fragment, gateway, in that order
cd alexa-skill && ask deploy   # then the skill
python scripts/preflight.py
```

Use `./deploy.sh functions --only relay` to deploy one function.

For Relay, the script publishes a version, waits until it is Active and moves the alias `live`. Relay's rollback is moving the alias back:

```bash
./deploy.sh relay-rollback            # lists versions and where "live" points
./deploy.sh relay-rollback <version>  # moves "live" there
```

The other three functions have no versions. To go back, check out the earlier commit, run `packages` and `functions` again.

If Detector's prompt, schema or RAG corpus changes, make the same change in `src/agents/detector.py` and in `shipagentcore/app/ship_diagnostician/main.py`, then run `agentcore deploy --yes` from `shipagentcore/`. `tests/test_taxonomy_consistency.py` catches the two copies drifting apart.

### One-off data fixes

Both scripts show what they would change unless you pass `--apply`. Both are safe to run twice. A deployment that has only ever run the current code has nothing for them to do.

| Script | When it applies |
|---|---|
| `scripts/migrate_decision_names.py` | Findings decided before decisions were named allowed and blocked hold `approved` or `rejected`. Run it after the new code is deployed. |
| `scripts/backfill_fragment_keys.py` | Findings recorded before code fingerprints existed have none, so blocking one would not hold the merge. It reads pull request diffs with `gh`. |

### Scripts you run by hand

- `python scripts/healing_loop.py` re-fetches each regulation source and checks that the text in `rag_corpus/` is still there verbatim. It exits with status 1 and lists the files that drifted.
- `python scripts/pattern_miner.py` turns newly allowed findings into patterns for Detector, stored in `ship-learned-patterns`. A blocked finding is never mined.

Neither runs on a schedule.

## 12. Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| Moving the Relay alias fails, or a new Relay version is not usable. `State` is `Pending`. | SnapStart takes its snapshot when a version is published. | Wait until `aws lambda get-function-configuration --function-name ship-relay --qualifier <version> --query State` says `Active`, then move the alias. `deploy.sh` waits for this itself. |
| No Echo chime, and the fragment processor log says nobody has opened the skill, or the credentials are missing. | The notifications permission was not granted, or the skill's credentials are not in Parameter Store. | Say "Alexa, ask ship review to turn on alerts" and allow it in the Alexa app. Check that `/ship/alexa/client_id` and `/ship/alexa/client_secret` exist (section 5). |
| A browser screen gets no notification. | The page was not registered with "Get alerts", or the browser blocked notifications. | Open the display page, tap "Get alerts on this screen" and allow notifications. `preflight.py` shows which names are registered. |
| A new WebSocket route or a changed integration does nothing. | The `prod` stage has no auto-deploy, so API Gateway still serves the old deployment. | Run `./deploy.sh websocket`. It ends with a new deployment. |
| The display page still has the old `WS_URL` or VAPID key after you pushed. | The Pages publish has not finished, or the browser cached the page. | Wait for the Pages build, then reload the page ignoring the cache. |
| The webhook Function URL answers 403 Forbidden. | The public invoke permission is missing. | Run `aws lambda get-policy --function-name <name>` (add `--qualifier live` for Relay). `deploy.sh functions` adds the statement. |
| Relay answers 421 "Invalid Host header" or 403 "Invalid Origin header". | The Host is not in `SHIP_RELAY_ALLOWED_HOSTS`, or the Origin is not in `SHIP_RELAY_ALLOWED_ORIGINS`. | Check both in Relay's configuration. `./deploy.sh functions --only relay` sets the host from the Function URL. Put your Pages origin in `.env.deploy` for the other. |
| GitHub shows a 404 or 405 for the webhook delivery. | The Payload URL lacks the route. | Use the Function URL plus `api/v1/webhook`. |
| GitHub shows a 403 saying the repo is not connected. | The repository is not in `ship-repos`. | Connect it in Gate (section 7). |
| The pull request check stays `pending` and Gate shows a "stuck" badge. | The SQS trigger is disabled, or reviews are failing and landing in the dead-letter queue. | `aws lambda list-event-source-mappings --function-name ship-fragment-processor` should show `Enabled`. `preflight.py` reports the dead-letter queue depth. |
| Bedrock calls fail with an access or quota error. | Model access is not enabled, or the account's quota is too low. | Enable the models in the Bedrock console. A new account's quota can start at 0. Ask AWS Support to raise it. |

## 13. Not covered by `deploy.sh`

| Item | Where it is handled |
|---|---|
| Bedrock model access | Console, section 2 |
| Detector in AgentCore Runtime, and the runtime's own role | `agentcore deploy`, section 4.3 |
| The Alexa skill, its Lambda and that Lambda's role | `ask deploy`, section 5 |
| The Login with Amazon security profile | Console, section 5 |
| GitHub token, webhook, branch protection and Pages | GitHub, sections 2, 4.13, 4.14, 9 |
| Editing the values listed in section 3 | By hand |
| A customer-managed KMS key for the parameters | The parameters use the default key. A custom key needs `kms:Decrypt` on the roles that read them. |
