# Product Feedback

Per tool: what we used it for, what worked well, what needs work, how
onboarding felt, and whether we'd build with it again. Written from
direct experience during this submission window, not general
impressions. Every claim below traces to a specific incident, most of
them also detailed with full reproduction steps in
[`FRICTION_LOG.md`](FRICTION_LOG.md).

---

## AWS Builder mini-challenge: which services, and how

SHIP's entire review engine and voice/push layer runs on AWS, not as a
token integration to qualify for this challenge but because the
architecture genuinely needed each piece.

- **Amazon Bedrock** handles the model backend for Detector, SHIP's
  compliance reasoning agent. Both the model choice and the retry
  configuration were decided on measured evidence. We benchmarked Claude
  Haiku 4.5, Nova Pro, Nova Lite, Qwen3-235B, GLM-5, and DeepSeek-V3.2
  against the real adversarial fixtures (genuine violations plus
  deliberate false-positive look-alikes). All six scored identically on
  accuracy, so per-model request-per-minute quota (10/min for Claude
  models, 200/min for Nova Lite on this account) turned out to be the
  actual bound on review throughput. Nova Lite is the production model on
  evidence, not brand familiarity.
- **Amazon Bedrock AgentCore Runtime** is Detector's deployed runtime, a
  real managed agent runtime rather than a Lambda-hosted approximation.
- **Amazon Bedrock Titan Embeddings** handles RAG retrieval over the
  sourced regulation corpus (GDPR, EU AI Act, OWASP text).
- **AWS Lambda** runs four functions, each scoped to one job: the webhook
  (fast-ack only), the fragment processor (the actual model call, one
  fragment per invocation, SQS-triggered), Relay (the MCP server, with
  SnapStart enabled), and the device gateway (WebSocket
  connect/disconnect/register). Deliberately not one do-everything
  function. A Lambda's blast radius and IAM scope should match what it
  actually needs, not the union of everything the project does.
- **Amazon SQS** decouples fragment review from the webhook's fast-ack
  requirement, with a dead-letter queue for genuine failures and a
  concurrency cap that keeps parallel reviews within the account's real
  Bedrock rate limit.
- **Amazon DynamoDB** holds one table per access pattern, each narrow: idempotent alert persistence, which repos are watched
  and their received-vs-reviewed health, a precomputed release summary,
  which device is reachable under which name for the push path, which
  alert was shown on a screen recently, decisions staged by voice until
  an explicit "proceed", and patterns learned from dismissed findings. The summary table exists because Relay's voice
  path needs to be one `GetItem` regardless of how many findings exist.
  The half-second Alexa+ budget would not survive aggregating on
  read.
- **Amazon API Gateway (WebSocket APIs)** carries real, server-initiated
  push to a named display device. We chose push over polling on purpose.
  A genuine compliance event happens on the order of weeks, and a display
  polling every few seconds to catch that would spend nearly all its
  traffic finding nothing changed. An idle WebSocket connection costs
  nothing until there's something to say.
- **AWS IAM** gives every Lambda its own role, scoped to exactly what
  that function does. Relay's role, for instance, can push to a WebSocket
  connection and read two specific tables. Nothing else.

### Strands Agents SDK

**Used for:** orchestrating Detector, the tool-using agent that retrieves
grounded regulation text before classifying a fragment, assigning a risk
score, and drafting a remediation patch.

**What worked well:** the tool-calling model is direct and predictable.
Building a `@tool`-decorated retrieval function and getting an agent that
reliably calls it before answering took very little code. Swapping the
underlying Bedrock model (across the six-model benchmark above) required
no changes to the agent logic itself.

**What needs work:** documentation examples skew toward simple,
single-tool demos. Getting a clear picture of structured-output
validation behavior, what happens when the model returns a field the
schema didn't expect, took direct experimentation rather than reading it
somewhere.

**Onboarding:** straightforward. `pip install strands-agents`, real
package name confirmed against current docs on the first attempt, no
version confusion.

**Would we build with it again?** **Yes.** It did exactly what it needed
to with minimal ceremony, and the tool-calling contract held up under
real adversarial testing, 9 of 9 correct on genuine violations and
look-alikes, not just the happy path.

### Amazon Bedrock

**Used for:** all model inference (Detector's classification calls) and
embeddings (RAG retrieval).

**What worked well:** the adaptive retry mode
(`Config(retries={"mode": "adaptive"})`) genuinely solved the
account-wide rate-limit problem. Once configured, concurrent fragment
reviews backed off and retried automatically with no custom coordination
code needed.

**What needs work:** a brand-new account's path to a first successful
`InvokeModel` call runs through several distinct, differently-worded
gates in sequence. A missing `InvokeModelWithResponseStream` permission,
an unsubmitted "use case details" form, missing AWS Marketplace
subscription permissions (Anthropic models are delivered through
Marketplace under the hood, which isn't obvious from the API surface),
and a new-account daily token quota that silently defaults to zero until
support intervenes. Each produced a different, correctly-worded error and
each was genuinely solvable, but five sequential gates for a first call
is a lot for a hackathon's time budget, and none of them were mentioned
together on one onboarding page. Separately, per-model
request-per-minute quota varies by roughly 20x across models on the same
account, with no single page listing them side by side. We had to query
Service Quotas directly, per model, to build the comparison that drove
our model choice.

**Onboarding:** rough on a brand-new account specifically, smooth once
past that point.

**Would we build with it again?** **Yes.** The model quality and the
adaptive-retry story are genuinely good. The new-account activation path
is what we'd want fixed, not the runtime experience.

### Amazon Bedrock AgentCore Runtime / AgentCore CLI

**Used for:** deploying Detector as a real managed agent runtime,
separate from the request-handling Lambda.

**What worked well:** once deployed, invocation is simple and the
runtime handled real production traffic without incident.

**What needs work:** the `bedrock-agentcore-starter-toolkit` pip package
is deprecated in favor of a different, npm-distributed CLI
(`@aws/agentcore`). We only discovered this because the deprecated
package self-reports it at runtime, not from any earlier documentation we
found before starting. The new CLI also scaffolds a brand-new template
project rather than deploying existing code in place, so our actual
Detector logic had to be ported into its generated structure rather than
deployed as-is.

**Onboarding:** confusing at the very first step, which tool to even
install, and fine after that.

**Would we build with it again?** **Yes**, with the caveat that we'd
budget real time for the CLI-migration surprise if starting fresh today.

### AWS Lambda (including SnapStart)

**Used for:** all four of SHIP's compute functions.

**What worked well:** SnapStart genuinely works as advertised once
configured correctly. A restored invocation measured at 440ms against a
multi-second cold init, and it's accepted on `arm64` plus Python 3.12
without qualification, confirmed directly against the API rather than
assumed.

**What needs work:** SnapStart requires a published version or alias,
never `$LATEST`. Reasonable once known, but it changes the whole
deployment shape (a versioning and alias workflow from day one, not
bolted on later), and that requirement isn't obvious until you try to
enable it and get rejected. More substantially, pairing SnapStart's
"build once, reuse the container" model with the MCP Python SDK's
Streamable HTTP transport is a real incompatibility, not a configuration
flag. The SDK's session manager can only run its lifespan once per
instance, so a module-level app object, the natural way to write a Lambda
handler, crashes on the second request against a warm container. The fix
(build the ASGI app fresh inside the handler, per invocation) isn't
described in either Lambda's or the MCP SDK's own docs for this
combination. We found it by reproducing the crash directly, not by
reading a warning anywhere.

**Onboarding:** fine for a simple function. The SnapStart plus
streaming-SDK combination needed real experimentation to get right.

**Would we build with it again?** **Yes** for the compute model itself.
The streaming-transport-on-Lambda gap above is worth Amazon documenting
explicitly, since it isn't specific to our code. Any Streamable HTTP MCP
server on Lambda would hit the same wall.

### Amazon DynamoDB

**Used for:** all persistent state: alerts, watched repos, the
precomputed release summary, device connections.

**What worked well:** on-demand billing meant zero capacity-planning
decisions during a time-constrained build. Conditional writes made
idempotency (a webhook redelivery can't create a duplicate alert or
silently reopen a resolved one) straightforward to implement correctly.

**What needs work:** the boto3 DynamoDB API rejects native Python
`float`/`int` for its Number type and requires `Decimal` instead. It's a
real, easy-to-hit surprise the first time you write a numeric field, and
the error message doesn't immediately suggest the fix.

**Onboarding:** smooth.

**Would we build with it again?** **Yes**, no reservations.

### Amazon API Gateway (WebSocket APIs)

**Used for:** real-time push delivery of findings to a named display
device.

**What worked well:** the core connect/route/integrate/deploy flow is
genuinely simple once understood, and `post_to_connection` is a clean,
predictable API for server-initiated push.

**What needs work:** two things cost real debugging time. First, a
route's Lambda return value is **not** relayed back to the connecting
client by default. Sending any acknowledgment requires an explicit
`post_to_connection` call, which isn't the behavior we expected coming
from REST/HTTP API Lambda-proxy integrations, where the return value *is*
the response. Second, connections have a hard 10-minute idle timeout and
a 2-hour maximum duration, neither configurable. Those are reasonable
limits, but they mean any "ambient display" use case must build
reconnect logic from day one rather than as a later hardening pass, and
we only found the exact numbers by searching rather than seeing them
surfaced during setup.

**Onboarding:** the CLI-based setup (create-api, create-integration,
create-route, create-deployment, create-stage) is a lot of sequential,
easy-to-get-wrong steps for what is conceptually one resource. A
higher-level "give me a WebSocket API pointed at this Lambda" command
would have saved real time.

**Would we build with it again?** **Yes.** The two gaps above are
one-time knowledge, not recurring friction, but we'd want to know both
before starting rather than after a live test failed.

### Alexa+ MCP Toolkit / Alexa AI CLI

**Used for:** attempting to connect Relay to the real Alexa+ platform via
its documented CLI path.

**What worked well:** the documented MCP spec requirement (`2025-11-25`,
Streamable HTTP) is exact and matched what we'd already built
independent of Alexa+'s own docs. The web-simulator alternative for a
simulated experience is a genuinely well-designed accommodation for
exactly the situation we hit.

**What needs work:** the CLI setup (`npm install -g @alexa-ai/cli`,
followed by creating an IAM user, configuring a cross-account role
assumption into an Amazon-owned AWS account, CodeCommit git credentials,
and CodeArtifact npm authentication) is the most involved onboarding
sequence we hit anywhere in this project. After completing all of it
correctly, the actual blocker turned out to be a completely separate,
undisclosed prerequisite. The target IAM role's trust policy only allows
AWS accounts an Amazon Solutions Architect has already registered by
hand, a fact mentioned once, in passing, mid-page, with no heading
calling it out and no earlier indication anywhere in the account-creation
or overview pages that this gate exists at all. A brand-new developer
account, following the public docs exactly, cannot reach a working
`alexa-ai deploy` without an out-of-band relationship the docs never
surface as a prerequisite. Full reproduction in
[`FRICTION_LOG.md`](FRICTION_LOG.md).

**Onboarding:** the single roughest onboarding experience in this
project, and not because the mechanics were hard. The actual blocking
requirement was undiscoverable from the documentation until we were
already deep into unrelated AWS configuration work.

**Would we build with it again?** **Yes, via the simulated-experience
path**, which worked exactly as documented and cost none of the above. We
would not attempt the CLI/device path again without first confirming an
Amazon Solutions Architect relationship exists. That's the actionable
suggestion we'd make: state that requirement first, before any AWS setup
instructions, not mid-page after them.

---

### Alexa Skills Kit CLI and skill simulator

**Used for:** an Alexa Skill that stands in for an Alexa+ add-on. It turns
what Alexa hears into calls to Relay over MCP.

**What worked well:** `ask deploy` with the Lambda deployer created the
skill, its language model, the Lambda function, the function's role and
the Alexa invoke permission in one command, and the first run worked.
`ask smapi simulate-skill` runs a spoken sentence through Alexa's own
language understanding and our Lambda without a device, so the whole path
was verified before any hardware was involved. Amazon's documentation
states that a Lambda trigger carrying the skill ID already rejects any
other caller, so the skill needs no signature-checking code.

**What needs work:** the notification catalog has no generic alert type,
so a compliance alert has to be phrased as a message count. Details in
[`FRICTION_LOG.md`](FRICTION_LOG.md).

**Onboarding:** `ask configure` was self-explanatory, and linking the AWS
profile there meant no separate credential setup for the deploy.

**Would we build with it again?** **Yes.**

---

## Open Source mini-challenge

Deploying Relay (our MCP server) to AWS Lambda surfaced a real gap in the
MCP Python SDK's own deployment docs: nothing covers a serverless runtime
reusing one warm process across separate invocations, which hits the
SDK's single-use session manager error the moment a container is reused.
Filed as [issue #3590](https://github.com/modelcontextprotocol/python-sdk/issues/3590)
and a docs PR, [#3591](https://github.com/modelcontextprotocol/python-sdk/pull/3591),
adding it as a documented third cause in `deploy.md` alongside the two
the SDK already covers, with the per-invocation fix and a SnapStart-specific
note. Repo: [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk).
GitHub: [pandayv](https://github.com/pandayv).
