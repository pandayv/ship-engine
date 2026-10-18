"""
Free-form requests for the SHIP Alexa Skill.

Alexa's own language model only recognises the sentences it was taught, so a
person who says something natural but unexpected would hit a wall. This module
takes what they actually said and lets a language model (Amazon Nova Lite on
Bedrock) pick which of Relay's tools to call. That is the same job an Alexa+
add-on would hand to Alexa+.

The model proposes; this code decides. The model is a translator, never an
authority, so everything that matters is checked here in plain code:

  * It only ever sees four tools. The one-step "accept this risk" tool is NOT
    among them: in testing the model mapped "reject the first one" onto it,
    which would have approved the finding. Every decision goes through
    stage_decisions, then proceed.
  * A turn may stage a decision or proceed with one, never both. The person
    sees what was staged on their screen and says "proceed" in a later turn.
    Only the person's own confirmation applies a staged decision: it is handled
    here in code, and a model that calls proceed any other way is refused.
  * The model says "the second one"; this code turns that into a finding id. The
    number a person speaks is the number their screen showed, which Relay recorded
    when it displayed the findings (session["routing"]). A number that is not in
    it is refused with a plain sentence, and Relay is only ever sent finding ids, so
    a finding that arrived after the screen was drawn cannot be mistaken for the
    one the person pointed at.
  * A screen is used only if the person named it or used it last. A model that
    guesses "laptop" is stopped and asked "Which screen?".
  * What is spoken is the server's own sentence, not the model's paraphrase, so
    the model cannot claim an approval that Relay refused. The model's own words
    are spoken only when it used no tool (a question back, or a polite no).

The voice gate itself (the person must have seen the finding on a screen, and
must give a reason) is enforced by Relay, not here and not by the model.
"""

import logging
import re
import time
from collections import namedtuple

log = logging.getLogger(__name__)

# What to say, and what to put on a card in the Alexa app, for one turn.
Reply = namedtuple("Reply", ["speech", "card"], defaults=[None])

MODEL_TOOLS = ("release_status", "display_on", "stage_decisions", "proceed")
MAX_SPEECH_CHARS = 260
HISTORY_EXCHANGES = 3
MIN_SECONDS_FOR_MODEL = 3.0     # the model call times out after 3 s, so never start one with less than that left

SCREEN_WORDS = {
    "tv": ("tv", "television", "fire tv", "fire stick", "firestick", "living room"),
    "tablet": ("tablet", "ipad", "i pad", "galaxy", "galaxy tab", "samsung", "android"),
    "laptop": ("laptop", "computer", "macbook", "mac"),
    "fridge": ("fridge", "refrigerator"),
}
NO_SCREENS = ("No screen is set up right now. Open the SHIP display page on any device, "
              "or review it in the console. I've put the link in your Alexa app.")


def which_screen(connected):
    """The question to ask when the person has not said which screen. Only screens that
    are really connected are offered. connected is None when that could not be checked."""
    if connected is None:
        return "Which screen? The TV, the tablet, the laptop, or the fridge?"
    if not connected:
        return NO_SCREENS
    return "Which screen? I can see " + _join(connected) + "."


def not_connected(screen, connected):
    if not connected:
        return NO_SCREENS
    return f"I don't see a {screen} connected. I can see {_join(connected)}."


def _join(names):
    names = [f"the {n}" for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def console_card(url):
    """A card in the Alexa app with the review console link, for when no screen is set up."""
    return {"type": "Simple", "title": "SHIP review console",
            "content": f"Open this on any device and sign in to review the findings: {url}"}
LOOK_FIRST = "Take a look at them on a screen first, then tell me why."
ASK_REASON = "What's the reason?"
ASK_REASON_EACH = "What's the reason for each one?"
CONFIRM_OR_DROP = "Say yes to apply what's staged, or no to drop it."
NOTHING_STAGED = "There is nothing staged to apply."
SAME_TWICE = "You mentioned the same finding twice. Which decision did you mean?"
# Words that are the command itself, not a reason for it. "approve everything" is not a reason.
COMMAND_WORDS = {"approve", "approved", "accept", "reject", "rejected", "dismiss", "clear", "ignore", "allow", "confirm",
                 "block", "everything", "all", "any", "these", "those", "findings", "finding", "first", "second", "third",
                 "fourth", "fifth", "sixth", "1st", "2nd", "3rd", "pull", "request", "number", "on", "please", "go", "ahead",
                 "let", "through", "yes", "okay", "ok", "do", "set", "mark", "keep", "blocked", "two", "three", "both"}
# Also not reasons: where the finding is ("the first one on the laptop"), how to point at it, and
# the names of the things being operated.
POINTER_WORDS = {"seventh", "eighth", "ninth", "tenth", "last", "next", "other", "another", "item", "items", "ones",
                 "thing", "things", "anything", "pr", "repo", "repository", "screen", "ship", "alexa", "proceed",
                 "stage", "decision", "decisions", "tool", "four", "five", "six", "seven", "eight", "nine", "ten"}
MIN_REASON_WORDS = 2
FILLER_WORDS = {"a", "an", "the", "it", "its", "is", "are", "was", "to", "of", "and", "for", "this", "that",
                "i", "because", "so", "they", "them", "just", "one", "in", "at", "my", "me", "our", "we", "with", "as",
                "from", "then", "also", "too"}

SYSTEM_PROMPT = """You are the voice of SHIP, a compliance reviewer for pull requests, speaking through an Alexa device. The person talks naturally. Work out what they want, then use the tools. Never write your reasoning: reply with a tool call or with the words to speak.

Everything you say is spoken aloud: one or two short sentences, no lists, no markdown, no file names, citations or code.

What to do:
- Asked how things stand, whether anything is blocked, or whether they can ship: call release_status.
- Asked to see detail, or says yes to an offer to show it: call display_on. Use a screen the person named, or the last screen used. Never guess a screen: if there is neither, ask which one.
- If the findings are already on the screen (see below), do not show them again; carry on with what was asked.

Decisions. Two outcomes exist:
  approve = accept the risk, so the merge is unblocked (also: accept, clear, dismiss, ignore, false positive, let it through).
  reject = confirm the finding is real, so the pull request stays blocked until the code is fixed (also: confirm, real problem, needs a fix, keep it blocked).
- A decision needs two things: the person has seen the findings on a screen, and they gave a reason in their own words. If they have not seen them, say you need them to look first and offer to show them. If they gave no reason, ask for the reason. Ask before calling any decision tool; never call one with an empty or invented reason.
- Findings are referred to by the numbers on the screen, within a pull request. Speech recognition garbles numbers: "the 1st 1" means "the first one". A note after the person's words says what they have already seen on a screen. Trust that note: if it says they have seen the findings, do not ask them to look again, go on to the decision.
- Every decision, one finding or several, approve or reject, goes through stage_decisions with each finding's number (position), approved true or false, and its own reason. Nothing is applied by stage_decisions: the person confirms next. Do not call proceed yourself: when the person says yes to a staged decision, the system applies it.
- You only help with SHIP's release status and findings. For anything else, say so in one sentence.
- A tool's spoken_response is what the person will hear; never contradict it."""


# A plain "yes" / "proceed" is a confirmation. Handled in code, not by the model: the model
# once answered "yes go ahead" by staging the same decision again instead of proceeding.
CONFIRM_WORDS = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "proceed", "go", "ahead", "do", "it", "confirm",
                 "confirmed", "that's", "thats", "that", "is", "right", "correct", "please", "and", "now", "apply"}
CANCEL_WORDS = {"no", "nope", "cancel", "stop", "never", "mind", "don't", "dont", "wait", "hold", "on", "not", "yet"}


def _words(text):
    return re.findall(r"[a-z']+", (text or "").lower())


def is_confirmation(text):
    words = _words(text)
    return bool(words) and set(words) <= CONFIRM_WORDS and bool(set(words) & {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "proceed", "go", "confirm", "confirmed", "right", "correct", "apply", "do"})


def is_cancellation(text):
    words = _words(text)
    return bool(words) and set(words) <= CANCEL_WORDS | CONFIRM_WORDS and bool(set(words) & {"no", "nope", "cancel", "stop", "never", "wait", "hold", "don't", "dont", "not"})


WANTS_TO_SEE = re.compile(r"\b(show|see|view|display|put|send|pull up|look at|review|open)\b", re.I)


class OrchestratorUnavailable(Exception):
    """The model could not be used in time. The caller falls back to simple rules."""


# ------------------------------------------------------------------ helpers

def screens_named(text):
    """The screens a sentence mentions, as canonical names."""
    low = " " + re.sub(r"[^a-z ]", " ", (text or "").lower()) + " "
    return {name for name, words in SCREEN_WORDS.items() if any(f" {w} " in low for w in words)}


SCREEN_STOP = {w for words in SCREEN_WORDS.values() for phrase in words for w in phrase.split()}


def _tokens(text):
    """Lowercase words, "it's" -> "it", one-letter leftovers dropped."""
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", (text or "").lower().replace("\u2019", "'"))
    words = [w[:-2] if w.endswith("'s") else w for w in words]
    return [w for w in words if len(w) > 1]


def reason_is_theirs(reason, said_texts, screen_names=()):
    """True if a reason is built from words the person actually said (this turn or
    earlier ones), so a model cannot make one up on their behalf, and has real content
    of its own. The command ("approve the first one"), a number, and the name of a
    screen ("on the laptop", including whatever name a screen registered under) are not
    a reason, so they do not count toward having one."""
    not_content = FILLER_WORDS | COMMAND_WORDS | POINTER_WORDS | SCREEN_STOP | {"screen"}
    for name in screen_names or ():
        not_content |= set(_tokens(name))
    words = set(_tokens(reason)) - not_content
    if len(words) < MIN_REASON_WORDS:
        return False
    said = set()
    for text in said_texts:
        said |= set(_tokens(text))
    return len(words & said) / len(words) >= 0.6


def strict_bool(value):
    """True or False only for a real boolean or the words true/false. Anything else is
    None. Relay would read the string "false" as true, so a spoken reject could approve."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def clean_decisions(decisions):
    """The model's decisions with every field forced to its proper type, or None if any
    cannot be. Only position, approved and reason are kept: a finding id the model may
    have added is dropped, ids come from what Relay recorded (see stage_target)."""
    cleaned = []
    for d in decisions or []:
        if not isinstance(d, dict):
            return None
        approved = strict_bool(d.get("approved"))
        try:
            position = int(d.get("position"))
        except (TypeError, ValueError):
            return None
        if approved is None:
            return None
        cleaned.append({"position": position, "approved": approved, "reason": str(d.get("reason", "")).strip()})
    return cleaned


def routing_for(session, repo, pr_number):
    """The recorded routing entry for a repo and pull request, or None if that PR was
    never shown to this person."""
    for pr in session.get("routing") or []:
        try:
            if pr["repo"] == repo and int(pr["pr_number"]) == int(pr_number):
                return pr
        except (KeyError, TypeError, ValueError):
            continue
    return None


def decisions_by_id(routing_pr, decisions):
    """Turns spoken positions into finding ids against what Relay recorded as shown.
    Returns (decisions_with_ids, None) or (None, sentence_to_speak). Done in code, after
    the model call: the model is never trusted for an id."""
    by_position = {}
    for f in routing_pr.get("findings", []):
        try:
            by_position[int(f["position"])] = f["alert_id"]
        except (KeyError, TypeError, ValueError):
            continue
    mapped = []
    for d in decisions:
        alert_id = by_position.get(d["position"])
        if alert_id is None:
            return None, (f"I don't have a finding number {d['position']} that you've looked at on that pull request. "
                          "Which one did you mean?")
        mapped.append({"alert_id": alert_id, "position": d["position"], "approved": d["approved"], "reason": d["reason"]})
    if len({m["alert_id"] for m in mapped}) != len(mapped):
        return None, SAME_TWICE
    return mapped, None


def match_connected(screen, connected):
    """The name a connected screen registered under that the person's word refers to, or
    None. "tablet" matches a screen registered as "ipad" or "galaxy tab" through the same
    synonym list that understands speech, so the page's name and the person's word need
    not be spelled alike."""
    if not connected:
        return None
    wanted = screens_named(screen) | {screen}
    for name in connected:
        if name in wanted or (screens_named(name) & wanted):
            return name
    return None


def clean_speech(text):
    """What the model wrote, made safe to speak: no reasoning tags, no markdown,
    no line breaks, a sensible length."""
    text = re.sub(r"<thinking>.*?</thinking>", "", text or "", flags=re.S | re.I)
    text = re.sub(r"</?thinking>", "", text, flags=re.I)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SPEECH_CHARS:
        cut = text[:MAX_SPEECH_CHARS]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        text = cut[: end + 1] if end > 60 else cut.rstrip() + "."
    return text


# What the model is shown for a decision. Relay's own schema asks for a finding id, which the
# model never has; it points at findings by the number on screen, and code maps that to an id
# (decisions_by_id). Everything else about the tool (name, repo, pr_number) comes from Relay.
MODEL_DECISIONS_SCHEMA = {
    "type": "array",
    "description": "One entry per finding being decided.",
    "items": {"type": "object", "properties": {
        "position": {"type": "integer", "description": "The number shown on the screen, within the pull request."},
        "approved": {"type": "boolean", "description": "true to approve (accept the risk), false to reject (the finding is real)."},
        "reason": {"type": "string", "description": "The person's own reason, in their words."},
    }, "required": ["position", "approved", "reason"]},
}
MODEL_DESCRIPTIONS = {
    "stage_decisions": (
        "Record a decision for one or more findings of ONE pull request, by the number each has on the screen. "
        "Approve accepts the risk; reject confirms the problem and keeps the pull request blocked. Needs the "
        "person's own reason for each. Nothing is applied yet: the person confirms in their next turn."),
}


def tool_config(tool_defs, connected=None):
    """Bedrock tool definitions, built from what Relay itself advertises, limited
    to the four tools the model may use, with the screen names pinned to the
    screens that are really connected (or the usual names if that is unknown).
    The model never chooses a device (code uses the screen in use) and points at
    findings by number rather than id."""
    specs = []
    for tool in tool_defs:
        if tool["name"] not in MODEL_TOOLS:
            continue
        schema = {k: v for k, v in tool.get("inputSchema", {}).items() if k != "title"}
        schema["properties"] = {
            name: {k: v for k, v in prop.items() if k != "title"}
            for name, prop in schema.get("properties", {}).items() if name != "device"
        }
        schema["required"] = [r for r in schema.get("required", []) if r != "device"]
        if tool["name"] == "display_on":
            schema["properties"]["surface"] = {
                "type": "string", "enum": list(connected) if connected else list(SCREEN_WORDS),
                "description": "Which screen to put the findings on.",
            }
        if tool["name"] == "stage_decisions":
            schema["properties"]["decisions"] = MODEL_DECISIONS_SCHEMA
        description = MODEL_DESCRIPTIONS.get(tool["name"]) or (tool.get("description") or "")
        specs.append({"toolSpec": {
            "name": tool["name"],
            "description": description[:900],
            "inputSchema": {"json": schema},
        }})
    return {"tools": specs}


def screens_note(connected):
    if connected is None:
        return ""
    if not connected:
        return " No screen is connected right now."
    return " Screens connected right now: " + ", ".join(connected) + "."


def context_note(session, connected=None):
    """What the person has already seen, stated plainly and placed right after their
    words. The model gives far more weight to its latest message than to its
    instructions, so this is where it must go to be heeded."""
    screen = session.get("screen")
    routing = session.get("routing")
    pending = session.get("pending")
    if pending:
        return (f"[Note: a decision is already staged for repo {pending['repo']} pull request {pending['pr_number']} and "
                "is waiting for the person to confirm it. If they confirm, call proceed for that pull request. "
                "Do not stage it again unless they change the decision." + screens_note(connected) + "]")
    if routing:
        parts = []
        for pr in routing:
            numbers = ", ".join(str(f["position"]) for f in sorted(pr["findings"], key=lambda f: f["position"]))
            count = len(pr["findings"])
            parts.append(f"repo {pr['repo']} pull request {pr['pr_number']} ({count} finding{'s' if count != 1 else ''}, "
                         f"numbered {numbers})")
        return (f"[Note: the person HAS ALREADY SEEN these findings on the {screen or 'screen'}: "
                + "; ".join(parts) + ". Do not ask them to look again." + screens_note(connected) + "]")
    if screen:
        return f"[Note: nothing is on a screen right now. Last screen used: {screen}." + screens_note(connected) + "]"
    return "[Note: nothing has been shown on a screen yet, and no screen has been used." + screens_note(connected) + "]"


def history_messages(session):
    messages = []
    for user_said, assistant_said in session.get("history", [])[-HISTORY_EXCHANGES:]:
        messages.append({"role": "user", "content": [{"text": user_said}]})
        messages.append({"role": "assistant", "content": [{"text": assistant_said}]})
    return messages


def remember(session, user_said, assistant_said):
    history = list(session.get("history", []))
    history.append([user_said[:300], assistant_said[:300]])
    session["history"] = history[-HISTORY_EXCHANGES:]


# ------------------------------------------------------------- one turn

def _device_args(session):
    return {"device": session["screen"]} if session.get("screen") else {}


def _after_proceed(session, result):
    """What the session keeps once Relay has answered a proceed. The numbers on screen have
    moved on once anything was applied; the staged batch is finished with unless Relay says
    part of it could not be saved and is still staged (a retry is then a plain yes)."""
    if not isinstance(result, dict):
        return
    if result.get("performed"):
        session["routing"] = []
    if not result.get("pending_remaining"):
        session.pop("pending", None)


def run_turn(utterance, session, tool_defs, call_tool, converse, deadline, connected=None, console_url=""):
    """Handles one spoken request. Returns a Reply: the sentence to speak and, when no
    screen is set up, a card with the review console link.

    `session` is mutated: it carries the last screen used, where the findings
    sit (for pointing at them), and a short history. `converse(system, messages,
    tools)` is the model call and returns Bedrock's Converse response.
    """
    pending = session.get("pending")
    if pending and is_confirmation(utterance):
        result = call_tool("proceed", {"repo": pending["repo"], "pr_number": pending["pr_number"],
                                       **_device_args(session)})
        _after_proceed(session, result)
        speech = clean_speech((result or {}).get("spoken_response") if isinstance(result, dict) else str(result)) or "Done."
        remember(session, utterance, speech)
        return Reply(speech)
    if pending and is_cancellation(utterance):
        try:    # so the screen stops showing it as pending; the answer does not depend on this
            call_tool("cancel_pending", {"repo": pending["repo"], "pr_number": pending["pr_number"],
                                         **_device_args(session)})
        except Exception:  # noqa: BLE001
            log.exception("could not clear the staged decision on Relay")
        session.pop("pending", None)
        speech = "Okay, I won't apply it."
        remember(session, utterance, speech)
        return Reply(speech)

    # Asked to see something when no screen exists at all: say so plainly and point to the
    # console, without asking the model to improvise an answer.
    if connected == [] and WANTS_TO_SEE.search(utterance) and not screens_named(utterance):
        speech = NO_SCREENS
        remember(session, utterance, speech)
        return Reply(speech, console_card(console_url) if console_url else None)

    if deadline - time.time() < MIN_SECONDS_FOR_MODEL:
        raise OrchestratorUnavailable("not enough time left for the model")

    try:
        response = converse(SYSTEM_PROMPT, history_messages(session) + [
            {"role": "user", "content": [{"text": f"{utterance}\n\n{context_note(session, connected)}"}]}],
            tool_config(tool_defs, connected))
    except Exception as exc:  # noqa: BLE001 - any model failure means "use the simple rules"
        raise OrchestratorUnavailable(str(exc)) from exc

    blocks = response["output"]["message"]["content"]
    uses = [b["toolUse"] for b in blocks if "toolUse" in b]
    model_words = clean_speech(" ".join(b["text"] for b in blocks if "text" in b))

    if not uses:
        speech = model_words or "I didn't catch that. You can ask what's up, or say show it on the TV."
        remember(session, utterance, speech)
        return Reply(speech)

    allowed_screens = screens_named(utterance) | ({session["screen"]} if session.get("screen") else set())
    if connected and len(connected) == 1:
        allowed_screens |= set(connected)      # only one to choose from, so no need to ask
    staged_now = any(u["name"] == "stage_decisions" for u in uses)
    speech = None
    card = None

    for use in uses:
        name, args = use["name"], dict(use.get("input") or {})
        if name not in MODEL_TOOLS:
            continue

        if name == "proceed":
            # Applying a staged decision is the person's act, not the model's. The plain
            # confirmation is handled above before the model is asked; reaching here means the
            # model chose proceed on its own, which is never forwarded: nothing staged, an
            # utterance that is not a confirmation, or a decision staged in this very turn.
            if staged_now:
                continue            # the staging answer is what gets spoken
            if not pending or not is_confirmation(utterance):
                speech = CONFIRM_OR_DROP if pending else NOTHING_STAGED
                continue
            args = {"repo": pending["repo"], "pr_number": pending["pr_number"], **_device_args(session)}

        if name == "display_on":
            surface = str(args.get("surface", "")).strip().lower()
            wanted = screens_named(surface) | {surface}
            if not (wanted & allowed_screens):
                speech = which_screen(connected)
                card = console_card(console_url) if connected == [] and console_url else None
                continue
            if connected is not None:
                registered = match_connected(surface, connected)
                if registered is None:
                    speech = not_connected(surface, connected)
                    card = console_card(console_url) if not connected and console_url else None
                    continue
                surface = registered       # use the name the screen actually registered under
            args = {"surface": surface}

        if name == "stage_decisions":
            # Only on a repo and pull request that were actually shown to this person.
            try:
                target = (str(args.get("repo")), int(args.get("pr_number")))
            except (TypeError, ValueError):
                target = None
            shown = routing_for(session, *target) if target else None
            if shown is None:
                speech = LOOK_FIRST
                continue
            # Only with reasons in the person's own words.
            decisions = clean_decisions(args.get("decisions"))
            said = [utterance] + [earlier for earlier, _ in session.get("history", [])]
            if not decisions or any(not reason_is_theirs(d["reason"], said, connected) for d in decisions):
                speech = ASK_REASON_EACH if decisions and len(decisions) > 1 else ASK_REASON
                continue
            # The spoken number becomes a finding id here, against what Relay recorded as shown.
            mapped, refusal = decisions_by_id(shown, decisions)
            if mapped is None:
                speech = refusal
                continue
            # Rebuilt from scratch: only fields this code validated reach Relay.
            args = {"repo": target[0], "pr_number": target[1], "decisions": mapped, **_device_args(session)}

        result = call_tool(name, args)

        if name == "stage_decisions" and isinstance(result, dict) and result.get("staged"):
            session["pending"] = {"repo": args["repo"], "pr_number": args["pr_number"]}
        if name == "proceed":
            _after_proceed(session, result)
        if name == "display_on" and isinstance(result, dict) and (result.get("delivered") or result.get("notified")):
            session["screen"] = args["surface"]
            if result.get("delivered"):
                session["routing"] = result.get("routing") or []

        spoken = result if isinstance(result, str) else (result or {}).get("spoken_response")
        if spoken:
            speech = str(spoken)

    speech = clean_speech(speech or model_words) or "Done."
    remember(session, utterance, speech)
    return Reply(speech, card)


# ----------------------------------------------------- simple-rules fallback

def fallback_turn(utterance, session, call_tool, connected=None, console_url=""):
    """Used when the model is unavailable or out of time. Understands the two
    things people most often ask, and says so honestly for anything else.
    Returns a Reply."""
    text = (utterance or "").lower()
    screens = screens_named(text)
    if re.search(r"\b(show|put|send|display|throw|pull up)\b", text):
        screen = next(iter(screens), None) or session.get("screen")
        none = connected == [] and console_url
        if not screen:
            return Reply(which_screen(connected), console_card(console_url) if none else None)
        if connected is not None:
            registered = match_connected(screen, connected)
            if registered is None:
                return Reply(not_connected(screen, connected), console_card(console_url) if none else None)
            screen = registered
        result = call_tool("display_on", {"surface": screen})
        if isinstance(result, dict) and (result.get("delivered") or result.get("notified")):
            session["screen"] = screen
            if result.get("delivered"):
                session["routing"] = result.get("routing") or []
        return Reply((result or {}).get("spoken_response", "Okay.") if isinstance(result, dict) else str(result))
    if re.search(r"\b(status|blocked|blocking|blockers?|ship|going on|what'?s up|anything)\b", text):
        return Reply(str(call_tool("release_status")))
    return Reply("I can tell you what's blocked and put it on a screen. Try, what's up, or show it on the TV.")
