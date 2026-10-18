# Deciding findings by voice

This page describes how a spoken decision works today. The code behind it is
`src/relay/server.py` (`stage_decisions`, `proceed`, `cancel_pending`),
`src/storage/pending_store.py` and `src/storage/session_store.py` on the
Relay side, and `alexa-skill/lambda/orchestrator.py` on the skill side.

## The idea

Voice should be able to carry the work of reviewing, not only announce it.
But a decision made by voice must still be a decision a person made. The
person looked at the finding, said why in their own words, and confirmed.
Every rule below serves that one sentence.

Two steps make it work without a spoken read-back. The person's sentence
stages the decisions and shows them on the screen as pending. A plain "yes"
then applies them. Nothing is final until that second step, so a mistake
caught on the pending screen needs a correction, not an undo.

## What approve and reject mean

| Decision | Meaning | Effect on the pull request |
|---|---|---|
| Approve | The person accepts the risk. | The finding stops blocking. The `ship/compliance` check passes once nothing else blocks. |
| Reject | The finding is real and the pull request is not ready. | The check stays failing ("N findings rejected, fix the code") until a later commit no longer contains the flagged code. |

A rejected finding is decided, so it never comes back as something to
review. Voice still reports it: "Nothing needs a decision, but 1 rejected
finding still blocks the merge until fixed." The findings screen carries a
note for any pull request a rejected finding still holds, and Gate's history
view marks the row "Still blocking the merge".

The release is found by code, not by time. A rejected finding is released
when a commit is processed whose flagged fragments no longer include its
fragment (`alert_store.release_fixed_rejections`, called from the webhook).
A fragment that changed at all is judged again as a new finding.

## The flow

1. The person asks what is up. Relay answers from the precomputed summary.
2. The person asks for a screen. Relay pushes the findings and records every
   finding as shown, together with the number it had on that screen.
3. The person speaks a decision with a number and a reason.
4. The skill checks the sentence and turns each spoken number into a finding
   id, using the numbers Relay recorded as shown.
5. `stage_decisions` validates the whole batch, stores it, and pushes the
   screen so each row shows "Pending: approve" or "Pending: reject" with the
   reason. Alexa says: "Got it, take a look and say proceed when you're ready."
6. The person confirms with a plain yes. The skill calls `proceed`.
7. `proceed` re-checks every finding, writes each decision, comments on the
   pull request, recomputes the commit status and the summary, and refreshes
   the screen. Alexa says how many were applied and what was left alone.

Saying no, cancel or stop clears the staged batch ("Okay, I won't apply it.")
and the screen stops showing it as pending. Stopping the skill does the same.
A staged batch also expires after ten minutes on Relay, after which
`proceed` answers "Nothing pending to confirm."

## Who checks what

The model translates speech into a proposed call. It never sees finding text,
only counts and the numbers on screen. Plain code decides.

| Check | Where | Detail |
|---|---|---|
| Which tools the model can reach | Skill | `release_status`, `display_on`, `stage_decisions`, `proceed`. The one-step `request_risk_acceptance` is not among them. |
| A pull request that was shown | Skill | A decision is staged only for a repo and pull request Relay recorded as shown to this person. |
| A reason in the person's words | Skill | The reason needs at least two content words beyond the command, a number or a screen name, and at least 60 percent of them must appear in what the person said this turn or in the last three exchanges. |
| Number to finding id | Skill | The model gives a number. Code maps it to an id using the number each finding had on the screen that displayed it (`findings_routing`, `shown_position`). Relay is only ever sent ids. A number two seen findings both claim is dropped, not guessed. |
| A screen the person named | Skill | The screen must be one the person named, used last, or the only one connected. A guess is stopped and the person is asked "Which screen?". |
| Never stage and proceed together | Skill | A turn may do one or the other. The model calling `proceed` on its own is refused. Only a plain confirmation applies a staged batch. |
| Shown recently | Relay | Each finding must have been shown on a screen in the last ten minutes. An unreadable record counts as not shown. |
| A reason on record | Relay | Not empty, at most 500 characters. A long reason is refused, never cut. |
| The whole batch or nothing | Relay | One unseen, already decided or unknown finding refuses the entire batch. |
| Checked again at commit | Relay | `proceed` re-checks each finding, because the world changes between staging and confirming. |
| Decide once | Relay | The write is conditional on the finding still waiting. A second decision on the same finding is refused. |

## Referring to findings

Numbers are the way to point at a finding. The screen shows one table per
pull request, and the numbers restart in each one. Within a pull request,
blockers come first, then findings grouped by theme (data exposure, human
oversight, bias and fairness, agent capability), then by risk score. The
numbering is one flat sequence through the whole table, so "first" never
means two things.

The number a person speaks is the number the screen showed. Relay stores that
number with each finding when it displays it, so findings that arrive or get
decided afterwards cannot shift "the second one" onto a different finding.

Pointing at a finding by topic or severity ("the PII one") is not supported.
The model never sees finding text, so it cannot resolve such a phrase. A
number that was never shown gets this answer: "I don't have a finding number
N that you've looked at on that pull request. Which one did you mean?"

Decisions can be mixed, for example "first one is a false positive because
it's test data, second one is real because it leaks a phone number". Each
decision carries its own reason. Findings the person does not mention stay
untouched.

## When a sentence does not land

Guessing wrong is worse than not supporting the feature, so each failure has
its own plain answer.

| Situation | What Alexa says |
|---|---|
| Nothing has been shown yet | "Take a look at them on a screen first, then tell me why." |
| No reason | "What's the reason?" or "What's the reason for each one?" |
| The same finding named twice | "You mentioned the same finding twice. Which decision did you mean?" |
| Unclear approve or reject | "I couldn't tell whether that was an approve or a reject. Which was it?" |
| A finding not shown recently | "Finding 2 hasn't been shown on a screen recently. Take a look first." |
| A finding already decided | "Finding 2 was already decided in the console." |
| A finding that no longer exists | "Finding 2 is no longer there. Take a fresh look at the screen." |
| A reason over 500 characters | "That reason is too long. Please give it in a sentence or two." |
| No open findings on that pull request | "I don't see any open findings on pandayv/micro-finance #2 right now." |

## What `proceed` says

`proceed` reports one outcome per finding: applied, skipped (not shown
recently), already decided, not found, or error. The spoken sentence says so
honestly.

- All applied: "Done." for one, "Done. Applied all 3." for several.
- Some applied: "Applied 2 of 3. 1 already decided in the console."
- Every save failed: "I couldn't save that just now. Say proceed to try again."

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
  shown, the staged batch) lives in the Alexa session attributes held by the
  skill. Relay stays stateless.
- Changing your mind before confirming is restating the decision. A new
  staged set replaces the old one for that pull request.
- A batch covers one pull request. Bulk and numbered commands do not reach
  across pull requests.
- A decision that has been applied is final in this version. There is no
  reopen.
- Several findings may share one reason. That is by design.
- SHIP does not identify who is speaking. An alert goes to the person
  responsible for the release.
