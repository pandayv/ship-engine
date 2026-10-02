# Voice-driven multi-finding resolution — workflow

Planning doc, not yet built. First step before touching Relay or Gate's
template: agree on the experience across real scenarios, not just the
happy path, then design the mechanism to fit it.

## The problem this solves

Today, voice can confirm exactly one already-reviewed finding per turn,
in a fixed "approve it" → "why?" → reason dialogue. If a finding is
pushed to a screen with no input device, a TV, a kitchen display, there's
no way to act on more than one finding without a round trip per item.
That makes voice mostly a notifier: it can tell you something's wrong,
but it can't be how you actually clear your review. The point of putting
this on a voice assistant is that voice should be able to carry the
work, not just the announcement of it.

## Decision shapes a person actually uses

Not just "approve all" and "approve one, reject one":

- **Approve everything shown.** "We're good to move forward."
- **Reject everything shown**, the mirror case. "None of these are fine,
  they all need fixes."
- **Mixed, per finding.** "First one's a non-issue. Second one's an
  actual blocker."
- **Exclusion.** "Approve all of these except the second one." Probably
  the natural pattern once there are more than two or three findings,
  not enumeration.
- **Partial, explicit deferral.** "Just handle the first one for now,
  I'll get to the rest later." Has to leave the rest genuinely
  untouched, a real state, not an accident of the conversation ending.

## How a person refers to a finding

Ordinal ("first," "second") is the easy case, unambiguous given a
numbered screen. Real speech also reaches for:

- What it's about: "the PII one," "the one in the underwriting file"
- Severity: "the worse one," "the one that's actually risky"
- Anaphoric, referring back to what was just discussed: "that one," "the
  one you just read me the detail on." Only works if the conversation
  has short-term memory of what it just said, which lives in Alexa+'s
  own session, not in Relay (Relay stays stateless regardless).
- By exclusion: "not the first one, the other one"

Build for ordinal first. The design shouldn't *break* the moment someone
uses anything else, falling back to "I didn't catch which one you mean"
is fine; silently guessing is not.

## When a reference doesn't land cleanly

The category that matters most, because guessing wrong is worse than not
supporting the feature:

- "The third one" when only two exist. Has to say so, not fail silently.
- A description that could match two findings (two PII issues in the
  same file). Resolving the wrong one is a real, bad outcome for a
  compliance tool specifically, not a minor UX miss.

## Read back what happened, every time, success included

Settled: no separate confirm-before-acting step. That adds friction to
every single use to guard against a rare misunderstanding, exactly what
this feature is trying to reduce. Instead, every action gets a complete,
specific read-back after it executes, not a vague "okay, done":

> *"Alexa, I've reviewed these. We're good to move forward."*
> **"Done. All three approved, on the record: reviewed via voice,
> cleared to proceed."**

> *"Alexa, first one's a non-issue. Second one's an actual blocker, we
> need a fix."*
> **"Done. Finding 1 accepted: non-issue. Finding 2 confirmed, needs a
> fix."**

Silent success is as bad as silent failure. The read-back is what lets a
misunderstanding get caught and corrected immediately, in the same
conversation, instead of discovered later.

## Conversation continuity

- Changing your mind mid-flow ("wait, reverse that") has to be possible
  without starting the whole review over.
- Asking for a reminder of what something was without re-navigating to
  the screen ("what was wrong with the second one again?") is a natural
  ask mid-review.
- If a new finding lands *while* someone's mid-conversation, because
  Detector just finished judging another fragment, "second" can't start
  silently meaning something different than it did thirty seconds ago.
  The position list is frozen for the life of that review conversation.
  Anything that arrives mid-session waits to be numbered until the next
  "what's up."

## Scope: one PR at a time

Once "what's up" can report blockers across several PRs, "these" and
"first" get ambiguous at a higher level, findings in *which* PR? Voice
resolves against whatever's currently on screen for one PR at a time.
Bulk and ordinal commands don't reach across PRs. Matches how a person
actually thinks about it, one queue at a time.

## What stays exactly the same

Voice still can't approve something that was never shown on a screen.
Every finding still needs a reason, every time. This isn't a new path
around that rule, it's the same rule working across more than one
finding in a single conversation instead of being limited to one.

## What the screen has to show for this to work

Numbered order, not just a stack of cards. "First" and "second" only
mean something if the screen shows 1. and 2. explicitly, in the same
order voice will resolve them against. This is the same display change
the PR-grouped Gate redesign already needs, one UI change serves both,
not two separate pieces of work.

## What's actually new, mechanically

One new capability: given a repo and PR, list the findings currently on
screen for it, each with a stable position number, built from the same
data that already generates the screen push, not new logic, and frozen
for the duration of the conversation. Everything after that reuses the
existing approve/reject tool, called once per finding, with whatever
reason the person actually said. The existing safety check, a finding
must have been shown recently, doesn't change at all. It already works
per finding regardless of how voice found that finding's id.

## Settled

- **Bulk approvals recording an identical reason across every finding
  they touch are fine, by design.** The goal is reducing friction, not
  adding a "confirm the same reason applies to all of these?" step.
- **Who's speaking doesn't matter.** An alert goes to the person
  responsible for it. If other people are in the room and that person
  can't control who talks, that's not something SHIP needs to solve.
  Keep it simple.

## Still open

- **No way to undo a resolution today.** Status moves from frozen to
  resolved one-way; nothing currently reopens it. A good read-back
  should make a wrong resolution rare, but "rare" isn't "never." Worth
  deciding: build a narrow, tightly-scoped reopen path, or accept this
  as a manual-intervention edge case for now?
- **Does the Alexa+/Bedrock orchestration actually support making
  several tool calls inside one turn?** Needs verifying, not assuming,
  before any of the multi-finding scenarios above can work at all.
- **Stable sort order.** Risk score descending is the obvious default
  for numbering findings, worth confirming that's actually what a
  reviewer wants to see first.
