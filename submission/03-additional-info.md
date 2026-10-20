# Additional info page

Fields in the order Devpost shows them. **YOU** marks what only you can answer.

## Upload a file

Optional. Skip it.

## Submitter type, organization, country, Canada province

**YOU.** Organization: write "N/A" if you are not representing one. Province: "N/A" unless you live in Canada.

## Primary track

Alexa+

## Code repository URL

https://github.com/pandayv/ship-engine

It is public. GitHub detects the MIT license, and it shows in the About section.

## New or existing before August 31, 2026?

New. The first commit is dated September 3, 2026.

The follow-up box ("if existing, explain what you updated") does not apply. Write "N/A".

## AWS Builder Mini Challenge: yes

**Which AWS services did you incorporate and how?**

SHIP runs entirely on AWS.

- **Amazon Bedrock:** Amazon Nova Lite reviews code for compliance problems and picks tools for free speech. Titan embeddings find the right regulation text.
- **Amazon Bedrock AgentCore Runtime:** hosts the review agent, built with the Strands Agents SDK.
- **AWS Lambda:** runs five functions. The webhook, the fragment processor, the MCP server (with SnapStart), the screen gateway and the Alexa Skill.
- **Amazon SQS:** each risky piece of code is its own job, with a dead-letter queue for failures.
- **Amazon DynamoDB:** findings, decisions, what was shown on a screen, and registered screens.
- **Amazon API Gateway (WebSocket):** pushes findings to open screens.
- **AWS Systems Manager Parameter Store:** holds the push-notification key and the skill's credentials.
- **AWS IAM:** one role per function, with a reviewed policy for each.

How each one went, including what worked and what did not, is in `PRODUCT_FEEDBACK.md` in the repository.

## Open Source Mini Challenge: yes

**Contribution URL:** https://github.com/modelcontextprotocol/python-sdk/pull/3591

**Project repository URL:** https://github.com/modelcontextprotocol/python-sdk

**GitHub username:** pandayv

**What I did, how it works, and why it matters:**

SHIP's MCP server runs on AWS Lambda. It failed on its second request. The MCP Python SDK's session manager can start only once per process, and Lambda reuses a process across requests. The SDK's deployment guide covers two causes of this error, but not Lambda.

I opened issue #3590 and a docs pull request, #3591. The pull request adds reused-process runtimes such as Lambda as a third cause in the deployment guide. It explains the error and the fix: build the app inside the handler on every call. It also adds a note for SnapStart.

This matters because every Streamable HTTP MCP server on Lambda hits the same error on its second request. One paragraph in the guide saves each of those developers a day.

The repository's bot closed the pull request on the day I opened it. It keeps pull requests open only for contributors a maintainer has assigned to the issue. The issue is open. The change is on a public branch: https://github.com/pandayv/python-sdk/tree/docs/lambda-reused-process-deploy

SHIP itself is also a new open-source project under the MIT license: https://github.com/pandayv/ship-engine

## Feature requests (optional)

1. **Alexa proactive events: a generic alert (Important).** The catalog has eight fixed schemas and none for "something needs your attention." A compliance alert has to read like a message count. Add a generic alert with a short custom phrase.
2. **Alexa skills: raw text for open answers (Important).** An answer that matches no sample phrase reaches the skill with no text. Pass the raw words to the skill, or document slot elicitation as the way to ask open questions.
3. **MCP Python SDK: a serverless mode (Important).** Make Lambda and other reused-process runtimes work without a custom handler, and say so in the deployment guide.
4. **Bedrock: one page of per-model request limits (Nice-to-have).** Limits differ by 20 times across models on one account. We had to query each one to compare them.
5. **AgentCore CLI: deploy existing code in place (Nice-to-have).** The CLI creates a new template project, so existing code has to be ported into it.

## Friction log (optional, up to 10% bonus)

Paste the whole of `FRICTION_LOG.md`. It has 11 entries. Each one has the task, the steps, expected and actual results, a severity, the workaround and a suggestion.

If the box has a length limit, paste the three high-severity entries first. They are "Alexa+ MCP Toolkit: CLI deployment path requires an undisclosed partner-program gate", "Free text: a verb used as the carrier phrase is dropped", and "Questions: an answer that matches no sample phrase is lost".

## Project testing link and instructions (optional)

https://pandayv.github.io/ship-engine/alexa-simulator.html

You do not need an Echo or any login.

1. Open the simulator and press "What's up?". You see the sentence Alexa would say.
2. Press "Show here". The findings appear as one numbered table per pull request.
3. To see a screen receive findings, open https://pandayv.github.io/ship-engine/ship-display.html in a second tab and pick a name. Then pick that name in the simulator.
4. Open https://github.com/pandayv/micro-finance/pulls to see what SHIP posted: a comment per finding and a failing `ship/compliance` check.
5. https://pandayv.github.io/ship-engine/architecture.html shows every part.

The review console and the Echo flow are private. The video shows them. The deployment stays up through the judging period.

## Feedback question 1: Which tools, APIs and SDKs did you use, and for what?

- **Amazon Bedrock** (Nova Lite, Titan embeddings): code review, tool choice for voice, and regulation search.
- **Bedrock AgentCore Runtime and CLI:** hosts the review agent.
- **Strands Agents SDK:** builds the review agent and its tools.
- **AWS Lambda** (SnapStart, Function URLs): all the compute.
- **Amazon SQS:** one job per risky piece of code.
- **Amazon DynamoDB:** findings, decisions and registered screens.
- **Amazon API Gateway WebSocket:** live push to screens.
- **AWS Systems Manager Parameter Store:** keys and credentials.
- **MCP Python SDK:** the MCP server that Alexa talks to.
- **Alexa Skills Kit, ASK CLI and Skill Management API:** the voice skill, its deployment, and testing without a device.
- **Alexa Proactive Events:** the alert that tells you something needs a decision.
- **Alexa+ MCP Toolkit:** tried first, and blocked (see question 3).

## Feedback question 2: What worked well?

- **Strands Agents SDK:** tool calling is simple. Switching Bedrock models needed no change to the agent.
- **Bedrock:** adaptive retry mode handled the account's rate limits with no extra code.
- **AgentCore Runtime:** once deployed, invoking the agent was simple and steady.
- **Lambda SnapStart:** a restored invocation took 440 ms, against several seconds for a cold start.
- **DynamoDB:** on-demand billing meant no capacity planning. Conditional writes made retries safe.
- **API Gateway WebSocket:** `post_to_connection` is a clean way to push from the server.
- **ASK CLI:** `ask deploy` created the skill, its language model, the Lambda, its role and the invoke permission in one command. `simulate-skill` and `profile-nlu` let us test the whole voice path without a device.
- **Alexa documentation:** the MCP requirement was exact, and the simulated path was a fair option for builders without partner access.

## Feedback question 3: What needs work?

- **Bedrock, new accounts:** four separate gates stand between a new account and its first model call. No single page lists them. Request limits differ by 20 times across models, with no page that compares them.
- **AgentCore:** the pip toolkit is deprecated in favor of an npm CLI, and we only learned that from a runtime message. The new CLI creates a template project, so existing code has to be ported into it.
- **MCP SDK on Lambda:** a Function URL needs JSON responses, and the SDK's session manager runs once per process. SnapStart needs a published version. None of this is documented together. We reported it (issue #3590 and PR #3591).
- **API Gateway WebSocket:** a route's return value is not sent to the client. Connections drop after 10 minutes idle or 2 hours total, and neither limit can change. Setup takes many CLI steps for one resource.
- **Alexa skills:** there is no generic proactive alert. A verb used as a carrier phrase disappears from what the skill receives. An answer that matches no sample phrase arrives with no text. The utterance history API returned a server error.
- **Alexa+ CLI:** it needs an Amazon Solutions Architect to register your AWS account by hand. The documentation mentions this once, in the middle of a page.
- **DynamoDB:** the Python SDK rejects floats, and the error does not say to use `Decimal`.

## Feedback question 4: How was onboarding?

- **Strands, DynamoDB, ASK CLI:** smooth.
- **Lambda:** fine for a simple function. Hard with SnapStart and the MCP SDK together.
- **Bedrock:** rough on a brand-new account, smooth after the first call.
- **AgentCore:** confusing at the first step, which tool to install. Fine after that.
- **API Gateway WebSocket:** many steps for one resource.
- **Alexa+ MCP Toolkit:** the hardest onboarding in the project, and it ended at a gate the documentation does not name.

## Feedback question 5: Would you build with these again?

Yes.

The model quality, the managed runtime and the serverless parts did what we needed. Most of the pain was at the edges: a new account's first call, a CLI that changed, and a few limits learned the hard way. Each was a one-time cost.

We would skip the Alexa+ CLI path and start with a custom skill and the simulator.

## Agreement checkboxes

**YOU.** Age of majority, eligible jurisdiction, and not an employee or agent of the promotion entities.

## Which AI tools did you use while working on this project?

**YOU: confirm and edit.**

Claude Code, which runs Anthropic's Claude models, wrote most of the code, tests and documentation under my direction. Inside SHIP: Amazon Nova Lite and Titan embeddings on Bedrock, and the Strands Agents SDK. We also benchmarked Claude Haiku 4.5, Nova Pro, Qwen3-235B, GLM-5 and DeepSeek-V3.2.

## Describe the level of learning you or your team derived

**YOU: edit this into your own words.**

High. I come from risk and compliance, and I had built on Google's stack before. This project took me onto AWS: Bedrock and AgentCore, MCP servers, Alexa skills, and serverless design. The biggest lesson was that a rule enforced by a person or a server beats a rule written into a prompt.

## Did you gain AI value you can use in your career?

**YOU: edit this into your own words.**

Yes. I can now build an AI system where a person stays in charge of the decision, and I can show how it is enforced. That matters in any regulated field.
