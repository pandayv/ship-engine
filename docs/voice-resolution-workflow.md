# Voice-driven multi-finding resolution — workflow

Planning doc, not yet built. First step before touching Relay or Gate's
template: agree on the experience, then design the mechanism to fit it.

## The problem this solves

Today, voice can confirm exactly one already-reviewed finding per turn,
in a fixed "approve it" → "why?" → reason dialogue. If a finding is
pushed to a screen with no input device, a TV, a kitchen display, there's
no way to act on more than one finding without a round trip per item.
That makes voice mostly a notifier: it can tell you something's wrong,
but it can't be how you actually clear your review. The point of putting
this on a voice assistant is that voice should be able to carry the
work, not just the announcement of it.

## The experience

**Scenario A: everything's fine, move on**

> *"Alexa, what's up?"*
> **"3 blockers found on pandayv/micro-finance #4. Want it on a screen?"**
> *"Show me on the TV."*
>
> *[TV shows 3 numbered findings]*
>
> *"Alexa, I've reviewed these. We're good to move forward."*
> **"Done. All three approved, on the record: reviewed via voice,
> cleared to proceed."**

**Scenario B: mixed outcome, per finding**

> *"Alexa, what's up?"*
> **"2 blockers found on pandayv/micro-finance #7. Want it on a screen?"**
> *"Show me on the kitchen display."*
>
> *[Screen shows 2 numbered findings]*
>
> *"Alexa, I've reviewed them. First one's a non-issue. Second one's an
> actual blocker, we need a fix."*
> **"Done. Finding 1 accepted: non-issue. Finding 2 confirmed, needs a
> fix."**

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
data that already generates the screen push, not new logic. Everything
after that reuses the existing approve/reject tool, called once per
finding, with whatever reason the person actually said. The existing
safety check, a finding must have been shown recently, doesn't change at
all. It already works per finding regardless of how voice found that
finding's id.

## Open questions before building

- Does the Alexa+/Bedrock orchestration actually support making several
  tool calls inside one turn, for the multi-finding case? Needs
  verifying, not assuming.
- What's the stable sort order findings are numbered in, risk score
  descending seems like the obvious default, but worth confirming it
  matches what a reviewer would actually want to see first.
- Bulk approval ("we're good to move forward") records the same reason
  text against every finding it touches. Is a shared rationale
  acceptable for the audit trail, or should voice confirm each finding
  gets its own reason even in the "all fine" case?
