# IAM

Each SHIP Lambda runs under its own role. Each role holds one policy, named `<function>-access`,
that allows what that function does and nothing else. `deploy.sh roles` creates the four Lambda
roles and attaches their policies. `ask deploy` creates the Alexa skill's role, and
`deploy.sh skill-policy` attaches its policy.

## Roles

| Role | Function | What it does | Policy file |
|---|---|---|---|
| `ship-relay-role` | `ship-relay` | Relay, the MCP server. Reads findings, records decisions, pushes to screens. | `ship-relay-role.json` |
| `ship-webhook-AgentCore-lambda-role` | `ship-webhook` | The GitHub webhook and the Gate console. Queues review jobs, records decisions, keeps the check honest. | `ship-webhook-AgentCore-lambda-role.json` |
| `ship-fragment-processor-role` | `ship-fragment-processor` | Reviews one piece of code per run through Detector, records findings, sends the Alexa alert. | `ship-fragment-processor-role.json` |
| `ship-device-gateway-role` | `ship-device-gateway` | The WebSocket gateway. Registers screens and records what a page displayed. | `ship-device-gateway-role.json` |
| `ask-lambda-SHIPReview-*` | `ship-alexa-skill` | The Alexa skill. Remembers who to alert and picks Relay tools with Nova Lite. | `ask-lambda-SHIPReview-1791468806554.json` |

`roles.json` lists the same roles for the scripts. Every role can also write its own logs.

## What each role can do

**Relay** reads and updates findings and their summary, so it can report them and record
decisions. It records what each screen was shown, which is what lets the server require a look
before a voice decision. It holds staged decisions until a person confirms them, keeps track of
connected screens and their notification registrations, saves findings for a screen whose page is
closed, pushes to open pages through the WebSocket API, and reads the web push signing key.

**Webhook** sends review jobs to the queue. It reads findings and records decisions for the Gate
console, keeps the review summary and ledger that decide the pull request check, and manages the
list of connected repositories.

**Fragment processor** takes jobs off the queue and asks the AgentCore runtime to review each
piece of code. It writes the findings, updates the review ledger, marks the repository reviewed,
and reads the Alexa user list and the skill's credentials to send the alert.

**Device gateway** registers and forgets screen connections, files each screen's notification
address, hands over findings held for a screen that was closed, records that a page displayed
findings, and replies on the open connection.

**Alexa skill** remembers which user to alert, and calls Amazon Nova Lite to choose a Relay tool
for free speech.

The policy files list every resource and action exactly.

## Apply them

`deploy.sh roles` does this for you and fills in your account, region and WebSocket API id. To
apply a file by hand, replace the account id, region and WebSocket API id in it first. In
`ship-fragment-processor-role.json`, you can also replace `runtime/*` with your AgentCore
runtime's ARN.

## Permissions to run deploy.sh

The person running `deploy.sh` needs the policy in `deployer-policy.json`. It allows creating
and updating the tables, queue, parameters, functions, WebSocket API and roles that SHIP uses,
and passing those roles to Lambda. Fill in your account id and region, then attach it to the IAM
user that runs the script:

```
sed -e "s/ACCOUNT_ID/123456789012/g" -e "s/REGION/us-west-2/g" infra/iam/deployer-policy.json > deployer-policy.json
aws iam put-user-policy --user-name YOUR_USER --policy-name ship-deployer --policy-document file://deployer-policy.json
```

If an administrator creates the roles, they run `./deploy.sh roles`. Everyone else can then run
`./deploy.sh --skip-roles all` and needs only the rest of the policy.
