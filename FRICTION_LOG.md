# Friction Log — Build, Ship, Shape (Amazon AppDev 2026)

Captured live, while building, so entries are precise rather than
reconstructed from memory at submission time. Format follows the
hackathon's own friction-log guidance: task attempted, steps taken,
expected vs. actual, severity, workaround used, actionable suggestion.

---

## Alexa+ MCP Toolkit: CLI deployment path requires an undisclosed
## partner-program gate

**Task attempted:** connect Relay (our MCP server, already deployed and
spec-compliant on `2025-11-25` Streamable HTTP) to Alexa+ via the
documented `@alexa-ai/cli` path, per
`developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-quickstart.html`
and `set-up-your-development-environment.html`.

**Steps taken:**
1. Created an Amazon developer account. Free, self-service, worked as
   documented.
2. Followed the documented environment setup exactly. Created a scoped
   IAM user, configured an AWS CLI profile to assume
   `arn:aws:iam::372468808636:role/AddOn3PDeveloperToolsRead` (an
   Amazon-owned role), configured CodeCommit git credentials and
   CodeArtifact npm auth per the literal commands in the doc.
3. Ran `aws sts get-caller-identity --profile alexa-ai` to verify the
   role assumption, the doc's own suggested verification step.

**Expected:** a newly created AWS account and IAM user, following the
documented steps exactly, should be able to assume the documented role
and proceed to `alexa-ai configure` / `alexa-ai deploy`.

**Actual:** `AccessDenied` on `sts:AssumeRole`, every time, with no
propagation delay explaining it (retested after 20s). A second, more
targeted read of the setup page, closer than the first pass, surfaced the
actual requirement. It's stated once, in passing, with no heading calling
it out:

> "Log in to the AWS account that you provided to the Alexa Solutions
> Architect when you do these steps."

This is a partner-program gate. The target role's trust policy only
allows AWS accounts already registered by hand by an Amazon Alexa
Solutions Architect, a human relationship rather than a self-service
step. Nothing earlier in the onboarding flow (account creation, the
overview page, the quickstart page's own prerequisites list) discloses
that a live account ID must be exchanged with an Amazon employee before
the documented CLI steps will function. We have no such relationship and
no visible path to request one from the docs alone.

**Severity:** high. This blocks the entire CLI-based/real-device
integration path outright, for any developer who hasn't separately been
onboarded through an out-of-band partner channel. A brand-new developer
account, following the public docs exactly, cannot reach a working
`alexa-ai deploy` at all.

**Workaround used:** the Alexa+ track's own rules anticipate exactly this
gap and explicitly permit an alternative, a simulated Alexa+ web
experience with source code included. We pivoted to that path, building
a web-based voice-interaction simulation against Relay's real,
already-deployed, spec-compliant MCP server. This is a legitimate
submission path per the rules, not a workaround we invented, though we
only reached it after losing real time on the CLI path first.

**Suggestion:** state the Solutions-Architect/account-registration
prerequisite explicitly and prominently, ideally in the quickstart's own
"Prerequisites" section, before any AWS/IAM setup steps, not buried in a
single sentence mid-page on a different page. As written, a developer has
no way to know this gate exists until they've already done real AWS
configuration work and gotten a generic `AccessDenied` with no indication
of the actual cause.

---

## AWS vs. GCP: overall onboarding friction

Running note from the user's direct experience across this project and
its predecessor, not tied to one specific tool. The AWS/Amazon developer
experience across this project has repeatedly required multi-step,
cross-service credential choreography to reach a working state. Earlier
in this build: the multi-gate Bedrock model-access saga (use-case
submission, Marketplace subscription permissions, a new-account quota
held at a hard 0 until a support case resolved it), the AgentCore CLI's
own pip package being deprecated mid-build in favor of an unannounced npm
replacement, and a CDK bootstrap failure whose own rollback failed on a
second missing permission. Each was individually solvable, but the
cumulative pattern is real friction. Broad, cross-service IAM
choreography and undocumented prerequisite gates recur across unrelated
AWS product areas (Bedrock, AgentCore, and now Alexa+), and more than
once. By contrast, the user's independent experience with GCP for
comparable work (noted directly, not measured in this project) was
self-service and fast, with none of this multi-day credential
back-and-forth.

**Suggestion:** a single, consistent "what you need before you start, and
where to get each thing" page per product, stated once, up front rather
than discovered gate by gate, would meaningfully close this gap.
