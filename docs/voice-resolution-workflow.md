# Voice-driven multi-finding resolution — workflow

**Built and verified live** (2026-10-02): `stage_decisions` and `proceed`
are real Relay tools, deployed, exercised end to end against the live
system, a staged decision, confirmed pending on screen, committed with
proceed, and confirmed on GitHub (a real comment, a real commit-status
update on the commit the finding was found on). See
`src/relay/server.py` and `src/storage/pending_store.py`. This doc stays
as the design record; the "still open" items below are resolved, not
hypothetical.

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

## Stage on screen, confirm with "proceed"

Not a voice read-back. A read-back of several findings ("Finding 1
accepted... Finding 2 confirmed...") would strain or break the existing
one-sentence voice cap the moment there are more than two or three
findings, and it adds exactly the friction this feature is trying to
remove.

Instead: the voice utterance captures the decisions and reasons, but
doesn't resolve anything yet. It pushes the captured decisions to the
screen as **pending**, next to the findings they apply to, and voice
gives one short prompt:

> *"Alexa, I've reviewed these. First one's a non-issue. Second one's an
> actual blocker, we need a fix."*
> **"Got it, take a look and say proceed when you're ready."**
>
> *[Screen updates: Finding 1 shows "Pending: Accept — non-issue".
> Finding 2 shows "Pending: Needs fix — needs a fix".]*
>
> *"Proceed."*
> **"Done."**

Nothing is actually resolved until "proceed." This gets two things at
once: voice responses stay short regardless of how many findings are
involved, and a misunderstanding is caught by looking at the pending
screen and saying so, before anything is committed, not after. That
also means there's no "undo" problem to solve, correcting a pending
decision before commit isn't an undo, it's just not having said
"proceed" yet.

## Conversation continuity

- Changing your mind before committing is just restating the decision
  ("actually, finding 2 is fine too") and the pending screen updates
  again, still nothing resolved until "proceed."
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

Numbered order, not just a stack of cards, grouped by PR, and within a
PR:

1. **Blockers before review-only findings.** A blocking finding outranks
   a review-only one regardless of risk score, it's the one actually
   stopping the merge.
2. **Clustered by detector theme within that**, all the PII findings
   together, then the human-oversight-gap ones, and so on, which also
   makes "approve all the PII ones" a natural thing to say.
3. **Risk score descending as the final tiebreaker.**

The numbering itself stays one flat sequence straight through the whole
list, 1, 2, 3, regardless of the visual theme clusters, so "first" and
"second" are never ambiguous between "first overall" and "first in this
theme." "First" and "second" only mean anything if the screen shows
these numbers explicitly, in the same order voice resolves them against.
This is the same display change the PR-grouped Gate redesign already
needs, one UI change serves both, not two separate pieces of work.

## What's actually new, mechanically

One tool, given a repo and PR, that takes a list of decisions (position,
approve or reject, reason) and **stages** them rather than resolving
anything immediately: it writes a pending-disposition record and pushes
the updated screen state showing each touched finding as pending. A
second trigger, "proceed," reads that pending record and loops through
it, calling the existing approve/reject logic once per finding, same
safety check as always (must have been shown recently), then clears the
pending record. The model only ever has to construct one call with a
structured list, not make several separate tool calls in one turn,
removing a dependency on orchestration behavior that didn't need to be
there.

Finding order and numbering reuse the same data that already generates
the screen push, extended with the severity/theme/risk sort above, and
frozen for the duration of the conversation.

## Settled

- **Bulk approvals recording an identical reason across every finding
  they touch are fine, by design.** The goal is reducing friction, not
  adding a "confirm the same reason applies to all of these?" step.
- **Who's speaking doesn't matter.** An alert goes to the person
  responsible for it. If other people are in the room and that person
  can't control who talks, that's not something SHIP needs to solve.
  Keep it simple.
- **No undo needed.** Resolved by the stage-then-"proceed" design above,
  not by building a reopen mechanism. Nothing is final until "proceed,"
  so there's nothing to undo.
- **One tool handles the batch**, rather than depending on whether the
  Alexa+/Bedrock orchestration supports several tool calls in a single
  turn. Removes the dependency instead of needing to verify it.
- **Sort order: severity, then detector theme, then risk score, numbered
  flat.** See "What the screen has to show" above.
