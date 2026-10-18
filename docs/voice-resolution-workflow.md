# Deciding findings by voice

This page describes how a spoken decision works today. The code behind it is
`src/relay/server.py` (`stage_decisions`, `proceed`, `cancel_pending`),
`src/storage/pending_store.py` and `src/storage/session_store.py` on the
Relay side. On the skill side it is `alexa-skill/lambda/review_flow.py`
(the guided conversation and how it reads answers),
`alexa-skill/lambda/lambda_function.py` (the Alexa side) and
`alexa-skill/lambda/orchestrator.py` (free speech).

## The idea

Voice should be able to carry the work of reviewing, not only announce it.
But a decision made by voice must still be a decision a person made. The
person looked at the finding, said why in their own words, and confirmed.
Every rule below serves that one sentence.

Voice also has to feel like a conversation. SHIP asks one short question at
a time and takes the answer in plain words.

Two steps make it work without a spoken read-back. The person's answer
stages the decision and shows it on the screen as pending. A plain "yes"
then applies the batch. Nothing is final until that second step, so a
mistake caught on the pending screen is corrected before it is applied.

## What allow and block mean

| Decision | Meaning | Effect on the pull request |
|---|---|---|
| Allow | The person accepts the risk. | The finding stops blocking. The `ship/compliance` check passes once nothing else blocks. |
| Block | The finding is real and the pull request is not ready. | The check stays failing ("1 finding blocked by a reviewer, fix the code") until a later commit no longer contains the flagged code. |

A blocked finding is decided, so it never comes back as something to
review. Voice still reports it: "Nothing needs a decision, but 1 finding that
was blocked still holds the merge until the code is fixed." When only
non-blocking findings are waiting, the sentence ends with the same fact, for
example "2 issues found. 1 blocked finding still holds the merge." The
findings screen carries a note for any pull request a blocked finding still
holds, and Gate's history view marks the row "Still blocking the merge".

The release is found by code, not by time. A blocked finding is released
when a commit is processed whose flagged fragments no longer include its
fragment (`alert_store.release_fixed_blocks`, called from the webhook).
A fragment that changed at all is judged again as a new finding.

## The decision word

A decision is exactly the word `allow` or `block`. Each item passed to
`stage_decisions` is `{alert_id, decision, reason}`, and anything else in
`decision` is refused, never guessed at: a synonym, a different case,
surrounding spaces, a boolean. An `approved` argument is refused too, with
"Decisions no longer take "approved". Send decision as "allow" or "block"."
The finding is stored as resolved, with a `resolution` of `allowed` or
`blocked`.

People can say approve, accept, dismiss, clear or ignore, and the skill
understands each as allow. Reject, deny and confirm are understood as block.
The skill turns the spoken verb into the exact word before Relay sees it,
and answers in allow and block: "Allow, block, or skip?"

## The flow

The skill walks the person through the findings they have looked at. It
asks, the person answers, and each answer moves the conversation on.

1. **Status.** The person asks what is up, or opens the skill. Relay answers
   from the precomputed summary, for example "9 blockers found across 2 PRs.
   Ready for your decision. Want it on the laptop or the tablet?"
2. **A screen.** The person answers in words: "laptop", "the tablet please",
   "both", or "yes" when only one screen is set up. Relay pushes the findings
   and records every finding as shown, together with the number it had on that
   screen. Alexa says Relay's sentence and asks: "Want to go through them?"
   If the screen's page was closed, Relay sends a notification instead and
   Alexa says "Say ready once you can see them." The skill then asks Relay
   which findings were displayed, and starts only when some were.
3. **A finding.** On a yes, Alexa takes the findings on the person's screen
   in the order of their numbers: "Number 1 on pull request 1. Allow, block,
   or skip?" The answer may carry its reason: "allow it, it's only test
   data."
4. **A reason, if none was given.** Alexa asks "What's the reason? For
   example, test data, a false positive, or a risk you accept." For a block
   it asks "What's the reason? For example, it exposes personal data, or it
   needs a fix." Whatever the person says next is the reason.
5. **Staging.** Each decision is staged on Relay as soon as it is made. The
   screen shows the row as "Pending: allow" or "Pending: block" with the
   reason. Alexa says "Got it." and asks about the next finding.
6. **The end of a pull request.** "That's all for pull request 1: 1 allowed,
   1 blocked. Say yes to apply."
7. **Applying.** A yes calls `proceed`. Alexa says Relay's sentence ("Done."
   or "Done. Applied all 2.") and offers the next pull request: "Want to go on
   to pull request 2?" A no drops the batch ("Okay, I dropped them.").
   When nothing is left, Alexa says "That's everything you've looked at."

The questions, stage by stage:

| Stage | What Alexa asks | What it accepts |
|---|---|---|
| Where to look | "Want it on the laptop or the tablet?" (more than two screens: "Want it on a screen?") | A screen name or a common synonym ("computer" for the laptop, "iPad" for the tablet), "both", "all of them" or "everywhere", a yes when only one screen exists, or a no. A yes with several screens gets "Which one, the laptop or the tablet?" |
| Go through them | "Want to go through them?" | Yes or no, in the many ways people say them ("yeah", "go ahead", "let's start", "not now"). |
| After a notification | "Say ready once you can see them." | "Ready", "it's open", "I see it". If Relay has no record that the findings were displayed: "I don't see it open yet. Open it from the notification, then say ready." |
| A finding | "Number 1 on pull request 1. Allow, block, or skip?" | A decision, with or without a reason, for this finding or one the person names. |
| The reason | "What's the reason?" with examples | Anything that says why, or a change of mind ("actually, block it"). |
| Apply | "Say yes to apply." | Yes applies. No drops the batch. A decision on a numbered finding ("actually block number 2") goes back to the review. |
| The next pull request | "Want to go on to pull request 2?" | Yes or no. A no ends the review with "Okay. They're still open." |

When the person has looked at more than one pull request, or at more than
one finding, Alexa opens each pull request with "Pull request 1 has 2
findings on your screen."

### What the person can say

The skill understands more than the three decision words.

| The person means | Examples |
|---|---|
| Allow | allow, approve, accept, dismiss, clear, ignore. Softer: "it's fine", "looks good", "no problem", "let it through". A reason that leans: "test data", "false positive", "fake data", "already anonymized". |
| Block | block, reject, deny, confirm. Softer: "it's real", "needs a fix", "keep it blocked", "should not ship". A reason that leans: "it leaks", "it exposes", "unmasked". |
| Skip | skip, next, later, "not sure", "not now", "leave it for now". The finding stays open and is never staged. |
| Go back | go back, undo, "change that". Takes the last decision off, stages the rest (or clears the batch if nothing is left) and asks about that finding again. |
| Stop | stop, cancel, quit, "that's enough". With no decisions made, the review ends. With some, Alexa asks: "Say yes to apply them, or no to drop them." |
| Help, repeat | help, "what can I say"; repeat, "say that again", "which one". |
| A finding by number | "number 3, block it", "the second one is fine", "3rd 1 allow". |
| All of them | "allow all of them because they are test fixtures". Decides every open finding on the pull request with one reason. |

A strong word names the decision itself and outranks a soft one, so "allow
it, the data is not real" is an allow. A negation flips the word it touches:
"don't allow it" is a block and "don't block it" is an allow. An answer that
holds both an allow and a block word ("allow and block it") is not guessed
at. Alexa asks again. A bare yes or no is not a decision: Alexa asks the
question again.

The reason is the part of the answer left after the decision words, the
number and the filler at the front. "Allow, it's only test data" gives "only
test data". A reason with fewer than two content words ("fine") is asked for
once more: "Could you say a few words about why? For example, test data, or
a false positive." After that second ask the skill takes what was said, so
the review never loops. The record then shows the person's own words.

When Alexa cannot make sense of an answer it says "I didn't catch that." and
asks the question again. The second miss in a row adds "Say stop to leave."

### One sentence still works

The person can skip the conversation and decide in one sentence: "Allow the
first one on pull request 2 because it's only test data." This path goes
through the model. The steps are these.

1. The skill checks the sentence and turns each spoken number into a finding
   id, using the numbers Relay recorded as shown. A number the person says in
   words ("the second one") outranks the model's reading of it when a single
   finding is named. A bare yes never stages a decision: it only confirms
   one that is already staged.
2. `stage_decisions` validates the whole batch, stores it, and pushes the
   screen so each row shows its pending decision. Alexa says: "Got it, take a
   look and say proceed when you're ready."
3. The person confirms with a plain yes. The skill calls `proceed`.
4. `proceed` re-checks every finding, writes each decision, comments on the
   pull request, recomputes the commit status and the summary, and refreshes
   the screen. Alexa says how many were applied and what was left alone.

While a walk-through is under way, an explicit sentence is read as an answer
to the current question. It applies to the pull request being reviewed, and
each answer holds one decision, or one reason for all of them.

Saying no, cancel or stop to a staged batch outside the walk-through clears
it ("Okay, I won't apply it.") and the screen stops showing it as pending.
Stopping the skill does the same. A staged batch also expires after ten
minutes on Relay, after which `proceed` answers "Nothing pending to confirm."

## Who checks what

In the guided review, plain code reads every answer and no model is
involved. When the person speaks a full sentence instead, a model translates
it into a proposed call. It never sees finding text, only counts and the
numbers on screen. Either way, plain code decides.

| Check | Where | Detail |
|---|---|---|
| Only findings that were shown | Skill | The walk-through is built from `findings_routing`: the findings Relay recorded as seen on a screen. A finding never displayed is never asked about. |
| A bare yes decides nothing | Skill | In the walk-through a yes or no is a confirmation, never a decision. Alexa asks the question again. |
| An exact decision word | Skill and Relay | The skill reads the person's words into `allow` or `block` (or keeps only those two from the model). Relay refuses any other value for `decision`. |
| Which tools the model can reach | Skill | `release_status`, `display_on`, `stage_decisions`, `proceed`. The one-step `request_risk_acceptance` is not among them. The guided review calls `display_on`, `findings_routing`, `stage_decisions`, `proceed` and `cancel_pending` from code. |
| A pull request that was shown | Skill | A decision is staged only for a repo and pull request Relay recorded as shown to this person. |
| A reason in the person's words | Skill | In the walk-through the reason is the person's own answer, minus the decision words. It needs at least two content words beyond the command, a number or a screen name. A shorter one is asked for once more and then taken as said. For a full sentence handled by the model, at least 60 percent of the reason's words must appear in what the person said this turn or in the last three exchanges. |
| Number to finding id | Skill | The person's number, or the one Alexa asked about, is mapped to an id in code using the number each finding had on the screen that displayed it (`findings_routing`, `shown_position`). Relay is only ever sent ids. A number two seen findings both claim is dropped, not guessed. A number the person has not seen is refused. |
| A screen the person named | Skill | The screen must be one the person named, used last, or the only one connected. A guess is stopped and the person is asked "Which screen?". |
| Never stage and proceed together | Skill | A turn may do one or the other. The model calling `proceed` on its own is refused. Only a plain confirmation applies a staged batch. |
| Shown recently | Relay | Each finding must have been shown on a screen in the last ten minutes. An unreadable record counts as not shown. |
| A reason on record | Relay | Not empty, at most 500 characters. A long reason is refused, never cut. |
| The whole batch or nothing | Relay | One unseen, already decided or unknown finding refuses the entire batch. In the walk-through the review then ends with Relay's own sentence. |
| Checked again at commit | Relay | `proceed` re-checks each finding, because the world changes between staging and confirming. |
| Decide once | Relay | The write is conditional on the finding still waiting. A second decision on the same finding is refused. |

## How answers are captured

Alexa matches what a person says against the skill's sample phrases. A reply
that fits none of them reaches the skill as an unmatched request with no
text, so a free-form answer to a question ("it's only test data", "first
one is fine") ends in "I didn't understand" and the same question again.
Sample phrases cannot cover what people say to "why?".

The skill asks every question so that Alexa delivers the reply as plain words.

1. A step in `review_flow.py` returns a `Turn` with `elicit` set. The step
   states what to say, and `lambda_function.speak_turn` adds a
   `Dialog.ElicitSlot` directive to the response, with `slotToElicit` set to
   `answer` and `AnswerIntent` as the updated intent. Without `elicit`
   (the end of a conversation, a plain statement) no directive is sent.
2. In the interaction model, `AnswerIntent` has one slot, `answer`, of type
   `AMAZON.SearchQuery`. The slot has the sample `{answer}`, so it takes the
   whole utterance. Its elicitation is required, with the prompt "Go ahead.",
   and the dialog delegation strategy is `SKILL_RESPONSE`, so the skill
   drives the dialog rather than Alexa.
3. The next reply arrives as `AnswerIntent` with the person's words in the
   slot. `on_intent` hands them to `review_flow.answer`.
4. While a review is under way, a reply belongs to it even when Alexa
   picks another intent. Yes, no, stop, cancel, navigate home, help and the
   free-text intents (`AllowIntent`, `BlockIntent`, `AskIntent`, `MarkIntent`,
   `ShowFreeIntent`, `WhatIntent`) are turned into the same kind of answer,
   and `AMAZON.FallbackIntent` repeats the current question ("Sorry, I
   missed that. ... You can also say stop."). `StatusIntent`,
   `ShowIntent`, `StartReviewIntent` and `AlertsIntent` keep their own
   handling.
5. If the person stays quiet, Alexa says the `Turn`'s reprompt, which is the
   same question or a shorter form of it.
6. With no review under way, an `AnswerIntent` goes to the model like any
   other free speech. `StartReviewIntent` ("let's go through them") begins
   the walk-through from what Relay says was seen.

The meaning of an answer is then decided in code. `review_flow.py` holds the
vocabulary and one handler per stage (where to look, go through them, ready,
a finding, the reason, apply, the next pull request). Each handler accepts
whatever the person says: it either understands the words or asks again. A
model is not used here on purpose. A wrong reading of an answer must never
become a decision, and a fixed vocabulary keeps the meaning predictable.
`tests/test_review_flow.py` lists examples of what is understood.

The conversation lives in the session attributes under `flow`, small and
JSON-safe: the stage, the pull request, the queue of `[alert_id, number]`
pairs, the current index, the decisions so far (`[alert_id, number, decision,
reason]`), the skipped findings, a count of misses and the pull requests
still to offer. After every decision the whole set decided so far for the
pull request is sent to `stage_decisions`, which replaces the earlier batch.
When going back leaves nothing decided, the skill calls `cancel_pending`
instead. The spoken questions carry numbers and nothing else about a finding.

## Referring to findings

Numbers are the way to point at a finding. Alexa says the number when it
asks about a finding, and the person says it to jump to one. The screen
shows one table per pull request, and the numbers restart in each one. Within a pull request,
blockers come first, then findings grouped by theme (data exposure, human
oversight, bias and fairness, agent capability), then by risk score. The
numbering is one flat sequence through the whole table, so "first" never
means two things.

The number a person speaks is the number the screen showed. Relay stores that
number with each finding when it displays it, so findings that arrive or get
decided afterwards cannot shift "the second one" onto a different finding.

Pointing at a finding by topic or severity ("the PII one") is not supported.
Neither the model nor the walk-through sees finding text, so neither can
resolve such a phrase. A number that was never shown gets this answer in a
full sentence: "I don't have a finding number N that you've looked at on that
pull request. Which one did you mean?" In the walk-through it is: "I don't
have a number 9 that you've looked at." followed by the current question.

A full sentence can mix decisions, for example "allow the first one because
it's test data, block the second one because it leaks a phone number". Each
decision carries its own reason. Findings the person does not mention stay
untouched. In the walk-through each answer holds one decision, or one reason
for "all of them", because a sentence with both an allow and a block word is
asked again.

On the screen, a finding that blocks the merge is marked "Needs a decision"
and a non-blocking one "Flagged".

## When a sentence does not land

Guessing wrong is worse than not supporting the feature, so each failure has
its own plain answer.

| Situation | What Alexa says |
|---|---|
| Nothing has been shown yet | "Take a look at them on a screen first, then tell me why." In the walk-through: "I don't see anything on a screen that you've looked at yet. Say show it on a screen first." |
| No reason | "What's the reason?" or "What's the reason for each one?" |
| The same finding named twice | "You mentioned the same finding twice. Which decision did you mean?" |
| Unclear decision | "I couldn't tell whether to allow or block that. Which was it?" |
| A finding not shown recently | "Finding 2 hasn't been shown on a screen recently. Take a look first." |
| A finding already decided | "Finding 2 was already decided in the console." |
| A finding that no longer exists | "Finding 2 is no longer there. Take a fresh look at the screen." |
| A reason over 500 characters | "That reason is too long. Please give it in a sentence or two." |
| No open findings on that pull request | "I don't see any open findings on pandayv/micro-finance #2 right now." |
| An answer the walk-through cannot read | "I didn't catch that." and the question again. The second miss adds "Say stop to leave." |
| A bare yes or no to "Allow, block, or skip?" | The question again. |
| A reason too short | "Could you say a few words about why? For example, test data, or a false positive." Asked once, then the answer is taken as said. |
| The skill cannot check what was seen | "I couldn't check what you've looked at. Please try again." |
| Relay refuses a staged decision | Relay's own sentence, and the review ends. The skill does not retry. |
| "Ready" after a notification, but Relay has no finding recorded as displayed | "I don't see it open yet. Open it from the notification, then say ready." |

## What `proceed` says

`proceed` reports one outcome per finding: applied, skipped (not shown
recently), already decided, not found, unreadable, or error. The spoken
sentence reports what happened to each one.

- All applied: "Done." for one, "Done. Applied all 3." for several.
- Some applied: "Applied 2 of 3. 1 already decided in the console."
- Every save failed: "I couldn't save that just now. Say proceed to try again."
- Nothing readable: "I couldn't read what was staged, so I applied nothing.
  Please say your decisions again."

A staged row is unreadable when it holds no decision Relay can read, for
example one written by other code. It is never guessed at. Relay applies
nothing for that row and drops it, and the person says the decision again.

A finding that failed for a temporary reason stays staged with its original
clock, so a second confirmation retries it without extending the window. A
tool error is logged and never read aloud. The skill says "Something went
wrong on my side. Please try again in a moment." instead.

Status questions follow the same rule. If the summary cannot be read, Alexa
says "I can't check right now. Open the review console." It never says all
clear on a failed read.

## What a decision writes

For each applied finding Relay records the decision on the finding (with the
reason and `voice` as who decided), comments on the pull request, and then,
once per request, recomputes the `ship/compliance` status from stored alerts
and posts it to the pull request's current head. It refreshes the release
summary Relay speaks from. Each decision, refusal and cancel also writes one
audit line (tool, outcome, pull request, finding ids, a reason code, counts)
that carries no reason text and no finding text.

## Continuity and scope

- The conversation memory (the last three exchanges, which findings were
  shown, the staged batch, and the walk-through's place in its questions)
  lives in the Alexa session attributes held by the skill. Relay stays
  stateless.
- Changing your mind before confirming is restating the decision, or saying
  go back. A new staged set replaces the old one for that pull request.
- A batch covers one pull request. The walk-through takes the pull requests
  one after another and applies each on its own yes. Bulk and numbered
  commands do not reach across pull requests.
- If the person leaves the walk-through with decisions staged and not
  confirmed, they stay staged on Relay until they expire after ten minutes.
- A decision that has been applied is final in this version. There is no
  reopen.
- Several findings may share one reason. That is by design.
- SHIP does not identify who is speaking. An alert goes to the person
  responsible for the release.
