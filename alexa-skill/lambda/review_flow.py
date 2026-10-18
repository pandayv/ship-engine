"""
Guided review by voice: Alexa walks the person through the findings on their screen, one at a
time, and asks what to do with each.

Why a guided flow. Alexa only understands a sentence if it matches one of the skill's sample
phrases, so a free-form answer to a question ("it's only test data", "first one is fine") falls
to "I didn't understand" unless the skill asks the question in a way that makes Alexa deliver the
raw words. Every question this flow asks does that (see lambda_function.elicit), and this module
does the understanding in code, with a wide vocabulary and no model: a wrong reading of an answer
must never become a decision, so what each answer means is predictable.

What it keeps from the rest of SHIP:
  - only findings Relay says were shown on a screen are walked through;
  - a decision needs a reason in the person's own words;
  - each decision is staged on Relay (the screen shows it as pending) and nothing is applied until
    the person says yes at the end of a pull request;
  - a bare yes confirms, it never decides anything.

The conversation state lives in the session under "flow" and is small and JSON-safe.
"""

from __future__ import annotations

import re

import orchestrator as o

# ------------------------------------------------------------------ vocabulary

# Strong words name the decision itself. Weak words and phrases only suggest one, and are
# ignored when a strong word is present ("allow it, the data is not real").
STRONG_ALLOW = {"allow", "allowed", "approve", "approved", "accept", "accepted", "dismiss", "dismissed", "clear", "ignore"}
STRONG_BLOCK = {"block", "blocked", "reject", "rejected", "deny", "denied", "confirm", "confirmed"}
WEAK_ALLOW_WORDS = {"fine", "ok", "okay", "safe"}
WEAK_ALLOW_PHRASES = (
    "let it through", "let that through", "let them through", "no problem", "not a problem", "not an issue", "no issue",
    "looks good", "looks fine", "safe to ship", "go ahead",
)
WEAK_BLOCK_WORDS = {"real", "bad", "violation", "fail"}
WEAK_BLOCK_PHRASES = (
    "keep it blocked", "keep blocked", "keep them blocked", "needs a fix", "need a fix", "needs fixing", "must be fixed",
    "real problem", "real issue", "real leak", "should not ship", "shouldnt ship", "not okay", "not ok", "not fine",
)
# Reasons that imply the way a person is leaning, so "test data" alone is an allow with a reason.
ALLOW_REASON_HINTS = (
    "test data", "test fixture", "test fixtures", "false positive", "not real", "fake data", "dummy data", "sample data",
    "mock data", "already anonymized", "already anonymised", "accepted risk", "accept the risk", "known risk",
)
BLOCK_REASON_HINTS = ("leaks", "leaking", "exposes", "exposed", "unmasked", "not anonymized", "not anonymised", "real leak")

SKIP_WORDS = {"skip", "next", "later", "postpone"}
SKIP_PHRASES = ("not now", "leave it", "come back", "i dont know", "dont know", "not sure", "skip it", "skip this one")
STOP_WORDS = {"stop", "cancel", "quit", "exit", "enough", "pause"}
STOP_PHRASES = ("thats enough", "never mind", "nevermind", "im done", "i am done", "stop reviewing")
REPEAT_WORDS = {"repeat", "again", "pardon", "which"}
REPEAT_PHRASES = ("say that again", "say it again", "which one", "what number", "what was that")
BACK_WORDS = {"back", "previous", "undo"}
BACK_PHRASES = ("go back", "change that", "change the last one", "take that back")
HELP_WORDS = {"help"}
HELP_PHRASES = ("what can i say", "what are my options", "my options")
BULK_WORDS = {"all", "everything", "rest", "remaining"}
BULK_PHRASES = ("all of them", "the rest", "every one", "all of these", "all remaining")

YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "correct", "start", "ready", "proceed", "apply", "confirm",
             "please", "affirmative", "absolutely"}
YES_PHRASES = ("go ahead", "lets go", "let us go", "sounds good", "do it", "go on", "im ready", "its open", "i see it",
               "im looking", "i am looking", "i have it", "got it open", "lets start", "lets do it", "lets begin", "it is open",
               "i can see it", "i can see them", "i see them", "its up", "it is up", "its there", "it is there")
NO_WORDS = {"no", "nope", "nah", "later", "cancel", "stop", "never", "dont", "not", "negative"}
NO_PHRASES = ("not now", "not yet", "no thanks", "no thank you", "never mind", "ill pass", "maybe later")

# Words dropped from the front of an answer to find the reason inside it.
_LEAD = (STRONG_ALLOW | STRONG_BLOCK | WEAK_ALLOW_WORDS | SKIP_WORDS | BULK_WORDS
         | {"it", "its", "thats", "this", "that", "one", "ones", "number", "please", "and", "then", "so", "just", "because", "since",
            "as", "cause", "is", "the", "a", "an", "them", "these", "those", "of", "all", "for", "now", "i", "ill", "would",
            "like", "to", "say", "pull", "request", "on", "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
            "eighth", "ninth", "tenth"})

DECIDE = "decide"
REASON = "reason"
APPLY = "apply"
OFFER = "offer"
READY = "ready"
PICK_SCREEN = "pick_screen"
NEXT_PR = "next_pr"

MAX_MISSES = 2
HELP_TEXT = ("Say allow, block or skip. You can add a reason, like allow, it's test data. "
             "Say back to change the last one, or stop.")
ALLOW_EXAMPLE = "What's the reason? For example, test data, a false positive, or a risk you accept."
BLOCK_EXAMPLE = "What's the reason? For example, it exposes personal data, or it needs a fix."


class Turn:
    """What the skill says next. `elicit` means a question was asked: the skill then makes Alexa
    deliver whatever the person says next as plain words."""

    def __init__(self, speech, elicit=False, card=None, reprompt=None):
        self.speech, self.elicit, self.card = speech, elicit, card
        self.reprompt = reprompt or speech      # what is said again if the person stays quiet

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Turn({self.speech!r}, elicit={self.elicit})"


# ------------------------------------------------------------------ understanding

def _norm(text):
    t = o.normalize_spoken(text or "").lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _phrase(t, phrases):
    padded = f" {t} "
    return any(f" {p} " in padded for p in phrases)


def parse_confirm(text):
    """"yes" or "no" or None, for a question that asks for one."""
    t = _norm(text)
    words = set(t.split())
    if not words:
        return None
    if words & NO_WORDS or _phrase(t, NO_PHRASES):
        # "not" and "dont" only count as a no when nothing positive is said ("not bad" is rare here)
        return "no"
    if words & YES_WORDS or _phrase(t, YES_PHRASES):
        return "yes"
    return None


def parse_answer(text):
    """What the person meant by an answer to "allow, block, or skip?".

    Returns {"action", "reason", "bulk", "number"}. action is allow, block, skip, stop, help,
    repeat, back, or None when it could not be told. reason is the person's own words about why,
    if they gave some in the same breath. number is a finding number they named, if any."""
    out = {"action": None, "reason": "", "bulk": False, "number": o.spoken_position(o.normalize_spoken(text or ""))}
    t = _norm(text)
    words = t.split()
    wordset = set(words)
    if not words:
        return out

    if wordset & STOP_WORDS or _phrase(t, STOP_PHRASES):
        out["action"] = "stop"
        return out
    if wordset & HELP_WORDS or _phrase(t, HELP_PHRASES):
        out["action"] = "help"
        return out
    if len(words) <= 5 and (wordset & BACK_WORDS or _phrase(t, BACK_PHRASES)):
        out["action"] = "back"
        return out
    if len(words) <= 4 and (wordset & REPEAT_WORDS or _phrase(t, REPEAT_PHRASES)):
        out["action"] = "repeat"
        return out

    strong_allow = wordset & STRONG_ALLOW
    strong_block = wordset & STRONG_BLOCK
    # Negation flips the word it touches: "don't allow it" is a block, "don't block it" is an allow.
    if re.search(r"\b(not|dont|never|cannot|cant|shouldnt)\s+(allow|approve|accept|ok|okay|fine)\b", t):
        strong_allow, strong_block = set(), strong_block | {"block"}
    if re.search(r"\b(not|dont|never)\s+(block|reject|deny)\b", t):
        strong_block, strong_allow = set(), strong_allow | {"allow"}

    # Phrases that are reasons ("not real") must not be read as the weak word inside them.
    scrubbed = t
    for hint in ALLOW_REASON_HINTS + BLOCK_REASON_HINTS:
        scrubbed = scrubbed.replace(hint, " ")
    weak_allow = bool(set(scrubbed.split()) & WEAK_ALLOW_WORDS) or _phrase(t, WEAK_ALLOW_PHRASES) or _phrase(t, ALLOW_REASON_HINTS)
    weak_block = bool(set(scrubbed.split()) & WEAK_BLOCK_WORDS) or _phrase(t, WEAK_BLOCK_PHRASES) or _phrase(t, BLOCK_REASON_HINTS)

    if strong_allow and strong_block:
        return out                               # contradictory: ask again
    if strong_allow:
        out["action"] = "allow"
    elif strong_block:
        out["action"] = "block"
    elif wordset & SKIP_WORDS or _phrase(t, SKIP_PHRASES):
        out["action"] = "skip"
        return out
    elif weak_allow and weak_block:
        return out
    elif weak_allow:
        out["action"] = "allow"
    elif weak_block:
        out["action"] = "block"
    else:
        return out

    out["bulk"] = bool(wordset & BULK_WORDS) or _phrase(t, BULK_PHRASES)
    out["reason"] = _reason_in(text, t)
    return out


_PR_NUMBER = re.compile(r"\b(?:pull request|pr|p r|request)\s*(?:number\s*)?(\d{1,5})\b")


def pr_named(text):
    """The pull request number the person named ("pull request 7", "PR 7"), or None."""
    m = _PR_NUMBER.search(_norm(text))
    return int(m.group(1)) if m else None


def _is_number_word(token):
    """A finding number said as a digit or an ordinal ("3", "1st", "2nd"): pointing, not a reason."""
    return bool(re.fullmatch(r"\d{1,2}(?:st|nd|rd|th)?", token))


def _reason_in(text, normalized):
    """The part of an answer that says why: what is left after the decision words at the front."""
    tokens = normalized.split()
    i = 0
    while i < len(tokens) and (tokens[i] in _LEAD or _is_number_word(tokens[i])):
        i += 1
    rest = " ".join(tokens[i:])
    return rest if rest and o.reason_is_theirs(rest, [normalized]) else ""


def reason_from(text):
    """A reason given as the whole answer to "what's the reason?". Kept in the person's words,
    minus a leading "because"."""
    t = _norm(text)
    tokens = t.split()
    while tokens and tokens[0] in {"because", "since", "cause", "as", "its", "it", "is", "the", "reason", "my", "was"}:
        tokens = tokens[1:]
    return " ".join(tokens)


def reason_is_enough(reason, said):
    return o.reason_is_theirs(reason, [said])


# ------------------------------------------------------------------ the flow

def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def active(session):
    return bool(session.get("flow"))


def _queue_from(routing_pr):
    items = []
    for f in routing_pr.get("findings", []):
        try:
            items.append([f["alert_id"], int(f["position"])])
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(items, key=lambda x: x[1])


def _pr_list(routing):
    return [[pr["repo"], int(pr["pr_number"])] for pr in routing if _queue_from(pr)]


def _enter_pr(session, routing, repo, pr_number, first=True):
    pr = next((p for p in routing if p["repo"] == repo and int(p["pr_number"]) == int(pr_number)), None)
    queue = _queue_from(pr) if pr else []
    remaining = [p for p in _pr_list(routing) if not (p[0] == repo and p[1] == int(pr_number))]
    if not queue:
        session.pop("flow", None)
        return Turn("I don't have anything on that pull request that you've looked at.")
    session["flow"] = {"stage": DECIDE, "repo": repo, "pr": int(pr_number), "queue": queue, "i": 0, "decisions": [],
                       "skipped": [], "misses": 0, "remaining": remaining, "draft": None}
    intro = ""
    if len(queue) > 1 or not first:
        intro = f"Pull request {pr_number} has {_plural(len(queue), 'finding')} on your screen. "
    return Turn(intro + _question(session["flow"]), elicit=True)


def _question(flow):
    queue, i = flow["queue"], flow["i"]
    pos = queue[i][1]
    return f"Number {pos} on pull request {flow['pr']}. Allow, block, or skip?"


def begin(session, routing, pr=None):
    """Starts the walk-through over what the person has seen. `routing` is Relay's record of it.
    `pr` starts at a pull request the person named."""
    prs = _pr_list(routing)
    if not prs:
        session.pop("flow", None)
        return Turn("I don't see anything on a screen that you've looked at yet. Say show it on a screen first.")
    chosen = next((p for p in prs if p[1] == pr), None) if pr is not None else None
    if pr is not None and chosen is None:
        return Turn(f"I don't have pull request {pr} on your screen. " + _which_pr_text(prs), elicit=True,
                    reprompt=_which_pr_text(prs)) if session.get("flow") else Turn(f"I don't have pull request {pr} on your screen.")
    repo, pr_number = chosen or prs[0]
    return _enter_pr(session, routing, repo, pr_number, first=len(prs) == 1)


def _which_pr_text(prs):
    numbers = [str(p[1]) for p in prs]
    return "Which one, " + (numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " or " + numbers[-1]) + "?"


def offer_after_show(session, sentence, delivered, notified):
    """Called when findings were just put on a screen. Asks what comes next instead of waiting."""
    if delivered:
        session["flow"] = {"stage": OFFER, "misses": 0}
        return Turn(f"{sentence} Want to go through them?", elicit=True)
    if notified:
        session["flow"] = {"stage": READY, "misses": 0}
        return Turn(f"{sentence} Say ready once you can see them.", elicit=True)
    session.pop("flow", None)
    return Turn(sentence)


def offer_screens(session, screens, sentence):
    """After the status: the person was asked where to look. Their answer is read in code."""
    session["flow"] = {"stage": PICK_SCREEN, "screens": list(screens), "misses": 0}
    names = [f"the {s}" for s in screens]
    again = ("Want it on " + (names[0] if len(names) == 1 else " or ".join(names)) + "?") if len(names) <= 2 else "Want it on a screen?"
    return Turn(sentence, elicit=True, reprompt=again)


def _decided(flow, alert_id):
    return any(d[0] == alert_id for d in flow["decisions"])


def _next_index(flow):
    queue = flow["queue"]
    order = list(range(flow["i"] + 1, len(queue))) + list(range(0, flow["i"] + 1))
    for idx in order:
        aid = queue[idx][0]
        if not _decided(flow, aid) and aid not in flow["skipped"]:
            return idx
    return None


def _record(flow, alert_id, position, decision, reason):
    flow["decisions"] = [d for d in flow["decisions"] if d[0] != alert_id]
    flow["decisions"].append([alert_id, position, decision, reason])
    if alert_id in flow["skipped"]:
        flow["skipped"].remove(alert_id)


def _device(session):
    return {"device": session["screen"]} if session.get("screen") else {}


def _stage(session, ctx):
    """Stages everything decided so far on this pull request, so the screen shows it as pending
    and a partly finished review survives a pause. None if all is well, else a Turn to speak."""
    flow = session["flow"]
    if not flow["decisions"]:
        try:
            ctx.call_tool("cancel_pending", {"repo": flow["repo"], "pr_number": flow["pr"], **_device(session)})
        except Exception:  # noqa: BLE001 - clearing is bookkeeping
            pass
        session.pop("pending", None)
        return None
    batch = [{"alert_id": a, "position": p, "decision": d, "reason": r[:400]} for a, p, d, r in flow["decisions"]]
    result = ctx.call_tool("stage_decisions", {"repo": flow["repo"], "pr_number": flow["pr"], "decisions": batch, **_device(session)})
    if isinstance(result, dict) and result.get("staged"):
        session["pending"] = {"repo": flow["repo"], "pr_number": flow["pr"]}
        return None
    session.pop("flow", None)
    session.pop("pending", None)
    said = o.clean_speech((result or {}).get("spoken_response") if isinstance(result, dict) else "")
    return Turn(said or "I couldn't save that. Please try again.")


def _advance(session, ctx, lead=""):
    """Moves to the next undecided finding, or to the end of this pull request."""
    flow = session["flow"]
    nxt = _next_index(flow)
    flow["misses"] = 0
    flow["draft"] = None
    if nxt is not None:
        flow["i"] = nxt
        flow["stage"] = DECIDE
        return Turn(f"{lead}{_question(flow)}".strip(), elicit=True)
    return _summary(session, lead)


def _summary(session, lead=""):
    flow = session["flow"]
    allowed = sum(1 for d in flow["decisions"] if d[2] == o.DECISION_ALLOW)
    blocked = sum(1 for d in flow["decisions"] if d[2] == o.DECISION_BLOCK)
    skipped = len(flow["skipped"])
    if not flow["decisions"]:
        return _after_pr(session, f"{lead}You skipped all of pull request {flow['pr']}.".strip())
    flow["stage"] = APPLY
    parts = []
    if allowed:
        parts.append(f"{allowed} allowed")
    if blocked:
        parts.append(f"{blocked} blocked")
    if skipped:
        parts.append(f"{skipped} skipped")
    return Turn(f"{lead}That's all for pull request {flow['pr']}: {', '.join(parts)}. Say yes to apply.".strip(), elicit=True)


def _after_pr(session, lead):
    """This pull request is finished. Offers the next one, or ends."""
    flow = session["flow"]
    remaining = flow.get("remaining") or []
    if remaining:
        repo, pr_number = remaining[0]
        session["flow"] = {"stage": NEXT_PR, "repo": repo, "pr": pr_number, "remaining": remaining[1:], "misses": 0}
        return Turn(f"{lead} Want to go on to pull request {pr_number}?".strip(), elicit=True)
    session.pop("flow", None)
    return Turn(f"{lead} That's everything you've looked at.".strip())


def answer(session, text, ctx):
    """One answer from the person while a flow is active. Returns a Turn."""
    flow = session.get("flow")
    if not flow:
        return Turn("What would you like to do?")
    stage = flow["stage"]
    handler = {DECIDE: _on_decide, REASON: _on_reason, APPLY: _on_apply, OFFER: _on_offer, READY: _on_ready,
               PICK_SCREEN: _on_pick_screen, NEXT_PR: _on_next_pr}[stage]
    return handler(session, text, ctx)


def repeat(session):
    """The current question again, for when Alexa could not make sense of what was said."""
    flow = session.get("flow")
    if not flow:
        return None
    stage = flow["stage"]
    if stage == DECIDE:
        return Turn("Sorry, I missed that. " + _question(flow) + " You can also say stop.", elicit=True)
    if stage == REASON:
        return Turn("Sorry, I missed that. Just say a few words about why.", elicit=True)
    if stage == APPLY:
        return Turn("Sorry, I missed that. Say yes to apply, or no to drop them.", elicit=True)
    if stage == OFFER:
        return Turn("Sorry, I missed that. Want to go through them? Say yes or no.", elicit=True)
    if stage == READY:
        return Turn("Sorry, I missed that. Say ready once you can see them.", elicit=True)
    if stage == PICK_SCREEN:
        return Turn("Sorry, I missed that. " + _which_screen_text(flow), elicit=True)
    if stage == NEXT_PR:
        return Turn("Sorry, I missed that. Go on to pull request " + str(flow["pr"]) + "? Say yes or no.", elicit=True)
    return None


def _miss(session, spoken):
    flow = session["flow"]
    flow["misses"] = flow.get("misses", 0) + 1
    extra = " Say stop to leave." if flow["misses"] >= MAX_MISSES else ""
    return Turn(spoken + extra, elicit=True)


# --- offer / ready / screens


def _on_offer(session, text, ctx):
    answer_ = parse_confirm(text)
    named = pr_named(text)
    if answer_ == "yes" or named is not None:
        return begin(session, ctx.seen_routing(), pr=named)
    if answer_ == "no":
        session.pop("flow", None)
        return Turn("Okay. Say start whenever you want to go through them.")
    return _miss(session, "Want to go through them? Say yes or no.")


def _on_ready(session, text, ctx):
    answer_ = parse_confirm(text)
    if answer_ == "yes":
        routing = ctx.seen_routing()
        if routing:
            return begin(session, routing)
        return _miss(session, "I don't see it open yet. Open it from the notification, then say ready.")
    if answer_ == "no":
        session.pop("flow", None)
        return Turn("Okay. Say start whenever you're looking at them.")
    return _miss(session, "Say ready once you can see them.")


def _which_screen_text(flow):
    names = [f"the {s}" for s in flow.get("screens") or []]
    if not names:
        return "Which screen?"
    return "Which one, " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]) + "?"


def _on_pick_screen(session, text, ctx):
    flow = session["flow"]
    screens = flow.get("screens") or []
    low = _norm(text)
    named = [s for s in screens if s in o.screens_named(text) or s in low.split()]
    everything = bool(set(low.split()) & {"both", "all", "everywhere"}) or _phrase(low, ("all of them", "every screen"))
    confirm = parse_confirm(text)
    if everything and len(screens) > 1:
        session.pop("flow", None)
        return ctx.show_all()
    if len(named) == 1:
        session.pop("flow", None)
        return ctx.show(named[0])
    if len(named) > 1:
        session.pop("flow", None)
        return ctx.show_all()
    if confirm == "yes":
        if len(screens) == 1:
            session.pop("flow", None)
            return ctx.show(screens[0])
        return _miss(session, _which_screen_text(flow))
    if confirm == "no":
        session.pop("flow", None)
        return Turn("Okay. Say show it on a screen whenever you're ready.")
    return _miss(session, _which_screen_text(flow))


def _on_next_pr(session, text, ctx):
    flow = session["flow"]
    answer_ = parse_confirm(text)
    named = pr_named(text)
    routing = ctx.seen_routing()
    if named is not None:
        pr = next((p for p in _pr_list(routing) if p[1] == named), None)
        if pr is None:
            return _miss(session, f"I don't have pull request {named} on your screen. Go on to pull request {flow['pr']}? Say yes or no.")
        return _enter_pr(session, routing, pr[0], pr[1], first=False)
    if answer_ == "yes":
        return _enter_pr(session, routing, flow["repo"], flow["pr"], first=False)
    skipping = parse_answer(text)["action"] == "skip"
    if skipping:
        rest = flow.get("remaining") or []
        if rest:
            session["flow"] = {"stage": NEXT_PR, "repo": rest[0][0], "pr": rest[0][1], "remaining": rest[1:], "misses": 0}
            return Turn(f"Skipped. Want to go on to pull request {rest[0][1]}?", elicit=True)
        session.pop("flow", None)
        return Turn("Skipped. That's everything you've looked at.")
    if answer_ == "no":
        session.pop("flow", None)
        return Turn("Okay. They're still open.")
    return _miss(session, f"Go on to pull request {flow['pr']}? Say yes or no, or say skip.")


# --- deciding


def _on_decide(session, text, ctx):
    flow = session["flow"]
    named_pr = pr_named(text)
    if named_pr is not None and named_pr != flow["pr"]:
        # "allow the first one on pull request 2" while on pull request 1: the pull request the
        # person names wins. A decision must never land on a different pull request than the one said.
        if flow["decisions"]:
            return _miss(session, f"We're on pull request {flow['pr']}, and you've decided some of it. "
                                  f"Finish this one, or say stop first. " + _question(flow))
        routing = ctx.seen_routing()
        target = next((p for p in _pr_list(routing) if p[1] == named_pr), None)
        if target is None:
            return _miss(session, f"I don't have pull request {named_pr} on your screen. " + _question(flow))
        _enter_pr(session, routing, target[0], target[1], first=False)
        flow = session["flow"]
    parsed = parse_answer(text)
    action = parsed["action"]

    if action == "stop":
        return _stop(session, ctx)
    if action == "help":
        return Turn(HELP_TEXT + " " + _question(flow), elicit=True)
    if action == "repeat":
        return Turn(_question(flow), elicit=True)
    if action == "back":
        return _back(session, ctx)
    if action is None:
        # A bare yes or no here is not a decision: ask what they want to do with the finding.
        if parse_confirm(text) is not None:
            return _miss(session, _question(flow))
        return _miss(session, "I didn't catch that. " + _question(flow))

    # Aim at the finding they named, if any ("number 3, block it").
    named = parsed["number"]
    if named is not None:
        idx = next((k for k, q in enumerate(flow["queue"]) if q[1] == named), None)
        if idx is None:
            return _miss(session, f"I don't have a number {named} that you've looked at. " + _question(flow))
        flow["i"] = idx

    if action == "skip":
        aid = flow["queue"][flow["i"]][0]
        if aid not in flow["skipped"] and not _decided(flow, aid):
            flow["skipped"].append(aid)
        return _advance(session, ctx, "Skipped. ")

    reason = parsed["reason"]
    if not reason:
        flow["stage"] = REASON
        flow["draft"] = {"decision": action, "bulk": parsed["bulk"]}
        flow["misses"] = 0
        return Turn(ALLOW_EXAMPLE if action == o.DECISION_ALLOW else BLOCK_EXAMPLE, elicit=True)
    return _commit(session, ctx, action, reason, parsed["bulk"])


def _commit(session, ctx, decision, reason, bulk):
    flow = session["flow"]
    queue = flow["queue"]
    if bulk:
        for aid, pos in queue:
            if not _decided(flow, aid) and aid not in flow["skipped"]:
                _record(flow, aid, pos, decision, reason)
        lead = "Got it, all of them. "
    else:
        aid, pos = queue[flow["i"]]
        _record(flow, aid, pos, decision, reason)
        lead = "Got it. "
    refusal = _stage(session, ctx)
    if refusal is not None:
        return refusal
    return _advance(session, ctx, lead)


def _on_reason(session, text, ctx):
    flow = session["flow"]
    draft = flow.get("draft") or {}
    parsed = parse_answer(text)
    if parsed["action"] == "stop":
        return _stop(session, ctx)
    if parsed["action"] == "skip":
        flow["stage"] = DECIDE
        flow["draft"] = None
        aid = flow["queue"][flow["i"]][0]
        if aid not in flow["skipped"]:
            flow["skipped"].append(aid)
        return _advance(session, ctx, "Skipped. ")
    if parsed["action"] in (o.DECISION_ALLOW, o.DECISION_BLOCK) and parsed["action"] != draft.get("decision") and not parsed["reason"]:
        # "actually, block it": they changed their mind about the decision itself.
        draft["decision"] = parsed["action"]
        flow["draft"] = draft
        return Turn(ALLOW_EXAMPLE if parsed["action"] == o.DECISION_ALLOW else BLOCK_EXAMPLE, elicit=True)

    reason = parsed["reason"] or reason_from(text)
    if not reason_is_enough(reason, text):
        flow["misses"] = flow.get("misses", 0) + 1
        if flow["misses"] < MAX_MISSES:
            return Turn("Could you say a few words about why? For example, test data, or a false positive.", elicit=True)
        # Asked twice: take what they said rather than loop. The record shows their words.
        reason = reason_from(text) or _norm(text)
        if not reason:
            return _miss(session, "Say a few words about why, or say skip.")
    return _commit(session, ctx, draft.get("decision", o.DECISION_ALLOW), reason, bool(draft.get("bulk")))


def _back(session, ctx):
    flow = session["flow"]
    if not flow["decisions"]:
        return Turn("There's nothing to go back to. " + _question(flow), elicit=True)
    aid, pos, _, _ = flow["decisions"].pop()
    flow["i"] = next((k for k, q in enumerate(flow["queue"]) if q[0] == aid), flow["i"])
    refusal = _stage(session, ctx)
    if refusal is not None:
        return refusal
    flow["stage"] = DECIDE
    return Turn(f"Okay. {_question(flow)}", elicit=True)


def _stop(session, ctx):
    flow = session["flow"]
    if flow.get("decisions"):
        flow["stage"] = APPLY
        return Turn(f"Okay. You've decided {_plural(len(flow['decisions']), 'finding')} so far. Say yes to apply them, or no to drop them.", elicit=True)
    session.pop("flow", None)
    return Turn("Okay, we'll stop there.")


# --- applying


def _on_apply(session, text, ctx):
    flow = session["flow"]
    answer_ = parse_confirm(text)
    if answer_ == "yes":
        result = ctx.call_tool("proceed", {"repo": flow["repo"], "pr_number": flow["pr"], **_device(session)})
        o._after_proceed(session, result)
        said = o.clean_speech((result or {}).get("spoken_response") if isinstance(result, dict) else str(result)) or "Done."
        return _after_pr(session, said)
    if answer_ == "no":
        try:
            ctx.call_tool("cancel_pending", {"repo": flow["repo"], "pr_number": flow["pr"], **_device(session)})
        except Exception:  # noqa: BLE001
            pass
        session.pop("pending", None)
        return _after_pr(session, "Okay, I dropped them.")
    # The person may still be deciding: "actually block number 2" goes back into the review.
    parsed = parse_answer(text)
    if parsed["action"] in (o.DECISION_ALLOW, o.DECISION_BLOCK) and parsed["number"] is not None:
        flow["stage"] = DECIDE
        return _on_decide(session, text, ctx)
    return _miss(session, "Say yes to apply what you decided, or no to drop it.")
