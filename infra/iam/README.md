# IAM reference policies

One policy file per Lambda role, holding the permissions the code calls today and nothing else.
These are **reference files reviewed against the code. They have not been applied to AWS.**

| File | Role | Lambda |
|---|---|---|
| `ship-relay-role.json` | `ship-relay-role` | `ship-relay` (MCP server) |
| `ship-device-gateway-role.json` | `ship-device-gateway-role` | `ship-device-gateway` (WebSocket routes) |
| `ship-fragment-processor-role.json` | `ship-fragment-processor-role` | `ship-fragment-processor` (SQS consumer) |
| `ship-webhook-AgentCore-lambda-role.json` | `ship-webhook-AgentCore-lambda-role` (read from the Lambda's configuration; `deploy.sh` names a different role) | `ship-webhook` (GitHub webhook and the Gate console) |
| `ask-lambda-SHIPReview-1791468806554.json` | `ask-lambda-SHIPReview-1791468806554` | `ship-alexa-skill` |

## Applying one

Never overwrite an existing inline policy. The policy names in the console are not known from the
repo (`deploy.sh` names one that does not exist on the live fragment-processor role), so:

1. Pick a **new, unique** name for each, for example `ship-relay-reference-20261018`.
2. Check the name is free (`aws iam get-role-policy --role-name <role> --policy-name <name>` should
   say it does not exist) before putting it.
3. Put the whole file as that new policy. Compare it with what the role has today before removing
   anything older; removing the old inline policies is a separate, deliberate step.

## How each action was derived

Each line was checked against the call in the code, after the changes in this branch.

| Table or resource | Actions | Where |
|---|---|---|
| `ship-alerts` | GetItem, Scan, UpdateItem (Relay, webhook/Gate) | `alert_store.get_alert`, `_scan_all` (Scan, with `ConsistentRead`), `resolve_alert`, `release_fixed_rejections` |
| `ship-alerts` | + PutItem (fragment processor) | `alert_store.put_alert` creates the row; refresh and delivery steps are UpdateItem |
| `ship-status` | GetItem, PutItem (Relay) | `status_store.read_summary`, `refresh_summary` (conditional PutItem) |
| `ship-status` | + UpdateItem (webhook, fragment processor) | `review_ledger.fragment_reported` (UpdateItem `ADD`), `begin_review` (PutItem) |
| `ship-recent-reviews` | GetItem, PutItem | `session_store.recent_view`, `mark_shown` |
| `ship-pending-dispositions` | GetItem, PutItem, DeleteItem | `pending_store.get_pending_with_age`, `stage`, `clear_pending` |
| `ship-device-connections` | Scan, DeleteItem (Relay); UpdateItem, DeleteItem (gateway) | `device_store.list_*`, `remove_connection`, `register_connection` |
| `ship-push-subscriptions` | Scan, DeleteItem (Relay); PutItem (gateway) | `push_store.subscriptions_for_device`, `remove_subscription`, `save_subscription` |
| `ship-pending-display` | PutItem (Relay); GetItem, DeleteItem (gateway) | `push_store.save_pending`, `get_pending`, `clear_pending` |
| `ship-repos` | GetItem, PutItem, DeleteItem, Scan, UpdateItem (webhook); UpdateItem (fragment) | `repo_store.get_repo`, `connect_repo`, `disconnect_repo`, `list_repos`, `mark_event_seen`, `mark_reviewed` |
| `ship-alexa-users` | Scan (fragment); PutItem (skill) | `alexa_user_store.list_user_ids`; the skill's `remember_user` |
| `execute-api:ManageConnections` | Relay, gateway | `push.push_to_device`, `device_gateway_handler._ack` (`post_to_connection`) |
| SSM `/ship/webpush/vapid_private` | GetParameter (Relay) | `webpush._vapid_private_key` |
| SSM `/ship/alexa/client_id`, `client_secret` | GetParameter (fragment) | `alexa_notify._parameter` |
| SQS `ship-fragment-queue` | SendMessage (webhook); Receive/Delete/GetQueueAttributes (fragment) | `main._dispatch_fragments`; the event source mapping |
| `bedrock-agentcore:InvokeAgentRuntime` | fragment | `detector.detect_via_agentcore` |
| `bedrock:InvokeModel` on Nova Lite | skill | `lambda_function._converse` (Converse needs InvokeModel) |

## Left out on purpose

- **Bedrock `InvokeModel` and `ship-learned-patterns` on the webhook and fragment roles.** `deploy.sh`
  grants them. With `SHIP_DETECTOR_MODE=agentcore` the Lambdas only call the AgentCore runtime; the
  model and the pattern store are used inside the runtime, which has its own role. They are needed
  again only if a Lambda is switched to `in_process` detection.
- **Anything for the webhook's sequential no-queue fallback.** That path (`process_pr` with
  `SHIP_FRAGMENT_QUEUE_URL` unset) writes alerts and calls Detector; it is for local runs. A
  deployed webhook has the queue set.
- **`ship-device-connections` on the fragment processor**, which `deploy.sh` grants. No code path
  there reads it.
- **Table administration** (`CreateTable`, `DescribeTable`). The `create_table_if_not_exists`
  helpers are run by a person with their own identity.

## Not verified

- The AgentCore runtime ARN shape in `ship-fragment-processor-role.json` (`runtime/*`) is the
  documented form, not read from the account. Replace the wildcard with the real runtime ARN from
  the deploy output.
- That each SecureString uses the AWS-managed key. If a customer-managed key was used, the roles
  that read it also need `kms:Decrypt` on that key.
- The account id, region and WebSocket API id (`680160265218`, `us-west-2`, `9vkr6eelzd`) come from
  the project notes and the display page's `WS_URL`. Check them against the account before applying.
- Whether the live roles carry other permissions that something outside this repo relies on.
  These files describe what this repo's code needs.
