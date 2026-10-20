## Inspiration

Every AI feature a team ships is also a legal decision. Someone should sign off on it. Usually nobody does.

A prompt that carries a customer's Social Security number. A zip code that nudges a credit score. A model's guess that becomes the decision. Each one is a pull request away from production.

Small teams have no compliance hire. Reviewing every pull request by hand slows them down. Shipping blind puts customers at risk.

SHIP is a third way. It reviews every pull request, and it puts a person back in charge of the decision.

## What it does

SHIP reads every pull request the moment it opens. When it finds a compliance problem, it blocks the merge, names the regulation, and suggests a fix.

Then a person decides.

- **Allow** means "I accept the risk." The merge can go ahead.
- **Block** means "this is real." The merge stays blocked until the code changes.

Every decision needs a reason, and the reason goes on the pull request.

You decide in a web console or by voice. Voice gives the headline. The screen in front of you shows the detail.

> *"Alexa, ask ship review what's up."*
> **"7 blockers found across 2 PRs. Want it on the laptop or the tablet?"**
> *"Laptop."*
> **"Want to go through them?"** *"Yes."*
> **"Number 1 on pull request 11. Allow, block, or skip?"**
> *"First one is fine, it's only test data."*

You talk to it the way you would talk to a colleague. A screen is any browser, set up once. If its page is closed, SHIP sends a notification.

One rule runs through everything: **no decision without a look.** SHIP will not take a voice decision on a finding you have not seen on a screen.

Alexa+ add-ons are not open to hackathon builders yet. So an Alexa Skill stands in for one. It talks to the same MCP server an Alexa+ add-on would use.

## How we built it

SHIP is built from small parts. Each part does one job.

- **Review.** GitHub tells a Lambda about each pull request. Each risky piece of code becomes its own job on **Amazon SQS**. An agent on **Amazon Bedrock AgentCore**, built with the **Strands Agents SDK** and running **Amazon Nova Lite**, judges each piece against regulation text. **Titan embeddings** find the right rule.
- **The check.** SHIP sets the pull request's `ship/compliance` check. It turns green only when every piece is reviewed and nothing blocks the merge.
- **The server.** Relay is an **MCP server** on **AWS Lambda**. It enforces the rules, whoever asks.
- **Voice.** An **Alexa Skill** talks to Relay. Code, not a model, understands your answers during the review.
- **Screens.** An **API Gateway WebSocket** sends findings to an open page. **Web Push** reaches a closed one.
- **Memory.** **DynamoDB** holds findings and decisions. **Parameter Store** holds the keys.

## Challenges we ran into

**A careless "yes."** Anyone can say "approve it" without looking. So the server checks that the finding was shown on a screen first, and that a reason was given. A notification does not count as a look.

**Alexa loses free answers.** Alexa only understands sentences it has seen before. Our first version asked "what's the reason?" and then did not understand the answer. Now every question is asked so that Alexa hands back whatever you say, and our code reads it.

**Quiet must never look safe.** A review that has not finished must not look like a pass. A blocked finding must keep blocking. So the check stays pending until everything has reported, and a finding you blocked keeps the merge blocked.

**MCP on Lambda.** The server's default reply is a stream, and a Lambda URL cannot carry a stream. One JSON reply fixed it. A status answer now takes about half a second.

## Accomplishments that we're proud of

- It works from end to end on a fork of an open-source finance app. A pull request comes in. The check turns red. A person decides by voice. The check follows the decision.
- The voice feels like a conversation. You say "laptop", "first one is fine", "it's only test data".
- The key rules live in the server, not in a prompt. A model cannot talk its way around them.
- We chose the model by testing. On ten test cases, every model we tried got all ten right. Nova Lite was the fastest and has the highest request limit, so we chose it.
- It costs almost nothing when idle.

## What we learned

- Voice is for the headline and the decision. Screens are for the detail.
- Silence must never read as safe.
- The number of requests per minute, not accuracy, limits how many pull requests we can review.
- A voice skill must ask open questions in a way that captures the answer.

## What's next for SHIP

- Apply the suggested fix as a commit once a person allows it.
- Learn from decisions on a schedule, so the next review is sharper.
- Add sign-in for screens and the server.
- Move to a real Alexa+ add-on when they open to builders, and add Fire TV as another screen.
