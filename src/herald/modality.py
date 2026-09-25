"""
The modality contract: every Herald tool declares which surface its output
is for, and that declaration is ENFORCED here rather than left to the
calling assistant's judgment.

Why this exists. The obvious way to put SHIP on a voice assistant is to
let it read findings aloud. That is a bad product. Nobody wants a
two-minute monologue enumerating PII violations and article citations
when the same information is absorbed in a three-second glance at a
screen. Voice is good at one thing here — an ambient, hands-free "is
anything wrong, and where should I look" — and bad at everything after
that.

So the split is deliberate:

    SPEECH  one sentence, hard-capped. Counts and a destination.
            Never a list, never a citation, never a code fragment.
    SCREEN  the real payload. Structured, dense, scanned not heard.
    ACTION  routes a payload to a surface, or hands off to the console.

The cap below is the enforcement mechanism, and it is load-bearing. A
SPEECH tool physically cannot return a list, because a list cannot fit in
the budget and the tool raises if it tries. That means no future prompt,
no assistant's own initiative, and no well-meaning change to a tool's
implementation can quietly turn Herald into a narrator. The constraint
lives in the server, where the client cannot argue with it.

The same principle governs what is ABSENT: there is no tool here that
accepts a compliance risk. See src/herald/server.py for why that absence
is a feature rather than an unimplemented backlog item.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# Roughly one unhurried spoken sentence. Chosen as a spoken-duration
# budget (about six to seven seconds at Alexa's cadence), not a text
# length that happened to look tidy: the point is the listener's patience,
# so the unit that matters is time, and characters are just the proxy the
# server can actually check.
SPEECH_CHAR_BUDGET = 180


class Modality(str, Enum):
    SPEECH = "speech"
    SCREEN = "screen"
    ACTION = "action"


class ModalityViolation(RuntimeError):
    """Raised when a tool's output does not fit the surface it declared.

    Deliberately an error rather than a silent truncation. Truncating a
    spoken response mid-sentence would produce exactly the bad experience
    this contract exists to prevent, and would hide the fact that a tool
    is returning the wrong shape for its surface.
    """


@dataclass(frozen=True)
class Spoken:
    """A response destined for a voice surface.

    Carries an optional `screen_hint` — where the detail actually lives —
    because the whole point of a speech response is to be brief AND to
    tell the listener where to look next.
    """

    sentence: str
    screen_hint: str | None = None

    def __post_init__(self) -> None:
        text = self.sentence.strip()
        if not text:
            raise ModalityViolation("a spoken response cannot be empty")
        if len(text) > SPEECH_CHAR_BUDGET:
            raise ModalityViolation(
                f"spoken response is {len(text)} characters, over the "
                f"{SPEECH_CHAR_BUDGET}-character budget. Voice states a count and a "
                f"destination; the detail belongs on a screen. Return a SCREEN tool instead."
            )
        if "\n" in text:
            raise ModalityViolation(
                "a spoken response cannot contain line breaks — a multi-line answer is a "
                "list, and a list is the thing voice must never read aloud."
            )

    def to_text(self) -> str:
        if self.screen_hint:
            return f"{self.sentence} {self.screen_hint}"
        return self.sentence
