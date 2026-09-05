"""
Screener — SHIP's fast, zero-cost pre-filter (formerly called "Tier 1 / Sentry"
in early drafts; renamed to avoid confusion with the unrelated Sentry.io brand).

Runs on every commit. Regex/AST pattern matching only — no model calls, no
network calls, no cost. If nothing matches, the build passes in milliseconds.
If something matches, the fragment is isolated and handed to Diagnostician
(the semantic evaluator) for a real judgment call.

The Amazon Comprehend PII-NER pass (decided 2026-09-03, replacing the
originally-considered Presidio) is a SEPARATE, second-stage check that only
runs on fragments already flagged here — it is not part of this module, to
keep this module's zero-cost property intact. See src/agents/pii_scan.py
(not yet written) for that piece.
"""

import ast
import re
from dataclasses import dataclass, field

# Maps directly to the taxonomy in ship_roadmap.md:
# imports/egress/pii_data       -> PIIE-001 (Detector #1, must-ship)
# logging_sinks + pii_data      -> PIIE-002 (near-free extension, same GDPR Art. 32 grounding)
# cache_sinks + pii_data        -> PIIE-003 (near-free extension, same GDPR Art. 32 grounding)
# decision_mutation (+ imports) -> TLGP-002 (built 2026-09-04)
# bias_data                     -> ALBP-001 (built 2026-09-04, stretch goal)
# agentic                       -> TLGP-001 (built 2026-09-04, stretch goal)
SCREENER_TRIGGERS = {
    "imports": ["openai", "anthropic", "langchain", "llamaindex", "transformers", "autogen"],
    "egress": ["httpx.post", "requests.post", "client.chat.completions", "axios.post"],
    "pii_data": ["email", "ssn", "tax_id", "credit_card", "biometric", "phone_number",
                 # fintech-flavored additions per the MicroPyramid demo repo's actual
                 # Client model fields (blood_group, dob, mobile, pincode-as-PII context)
                 "blood_group", "date_of_birth", "mobile", "annual_income", "account_number"],
    # PIIE-002: PII written to insecure log streams (stdout, cloud logs)
    "logging_sinks": ["logging.info", "logging.debug", "logging.warning", "logging.error",
                       "logger.info", "logger.debug", "logger.warning", "logger.error",
                       "print(", "console.log"],
    # PIIE-003: PII stored in a cache/session store without row-level encryption
    "cache_sinks": ["redis.set", "cache.set", "memcache.set", "session[",
                     "redis_client.set", ".setex("],
    # TLGP-002: an AI/LLM output directly drives a high-risk decision (status
    # mutation) with no apparent human-review step in between. Loose signal —
    # co-occurrence with imports/egress is what actually matters; Diagnostician
    # does the real judgment on whether a human gate exists.
    "decision_mutation": [".status =", ".approved =", ".rejected =",
                           "= 'Approved'", '= "Approved"', "= 'Rejected'", '= "Rejected"'],
    "bias_data": ["gender", "ethnicity", "race", "zipcode", "pincode", "income_tier", "weights"],
    "agentic": ["subprocess.run", "eval(", "exec(", "os.system", "bind_tools", "Agent("],
}


@dataclass
class ScreenerResult:
    matched: bool
    matched_buckets: list = field(default_factory=list)
    matched_terms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "matched_buckets": self.matched_buckets,
            "matched_terms": self.matched_terms,
        }


def _compile_term_pattern(term: str) -> re.Pattern:
    # Independent review (2026-09-05) caught a real regression here: an
    # unconditional (?<!\w) / (?!\w) on BOTH sides breaks matching for any
    # term whose own first/last character is already punctuation, not a
    # word character — which several real trigger terms are ("print(",
    # ".setex(", "eval(", "exec(", "Agent(", "session["). Reproduced
    # directly: scan("print(applicant_summary)") returned matched=False,
    # because (?!\w) checks the character AFTER the literal "(" — which is
    # the call's own argument, a word character — and rejects. ".setex("
    # failed on the LEADING side too: (?<!\w) checked the character before
    # the term's leading ".", which in "cache.setex(" is "e" (word char),
    # and rejected a completely normal, correct usage.
    #
    # The boundary check's actual purpose is only to reject a term being
    # embedded INSIDE a longer identifier (the finding #63 false positives:
    # "race" inside "traceback", "mobile" inside "automobile") — that risk
    # only exists on a side where the term's own edge character is itself
    # a word character. If the term already starts/ends in punctuation
    # (".", "(", "["), that punctuation can't be "continued" as part of a
    # longer identifier in any of these languages, so no boundary
    # assertion is needed — or correct — on that side at all.
    prefix = r"(?<!\w)" if term[0].isalnum() or term[0] == "_" else ""
    suffix = r"(?!\w)" if term[-1].isalnum() or term[-1] == "_" else ""
    return re.compile(prefix + re.escape(term) + suffix, flags=re.IGNORECASE)


# finding #21: _term_matches() used to build a fresh pattern string (escape
# + concatenate) and call re.search() with it on every single call — for
# ~50 terms across 8 buckets, that's ~50 pattern (re)constructions per
# scan(), every time, for text that's known at module-load time and never
# changes. Precompiled once here instead; scan()/isolate_fragment() now
# look up the compiled pattern instead of rebuilding it.
_COMPILED_TERM_PATTERNS: dict[str, re.Pattern] = {
    term: _compile_term_pattern(term) for terms in SCREENER_TRIGGERS.values() for term in terms
}


def _term_matches(text: str, term: str) -> bool:
    """
    Shared match logic for a single trigger term against a text (finding
    #33: scan() and isolate_fragment() used to reimplement this
    independently and could disagree).

    finding #63: the old version was a plain re.escape(term) substring
    search with no actual boundary anchoring, despite a comment claiming
    "word-boundary-ish" matching. Confirmed real false positives in this
    exact fintech domain: "traceback"/"embrace" tripped bias_data's "race";
    "automobile" tripped pii_data's "mobile". Fixed with single-char
    lookaround (?<!\\w)...(?!\\w) instead of \\b, since several terms end in
    punctuation (e.g. "print(", "httpx.post") where \\b's word-char
    requirement on both sides doesn't behave as intended.

    Follow-up fix (independent review, 2026-09-05): the first version of
    this applied the lookaround unconditionally on both sides, which broke
    matching entirely for the realistic case — a punctuation-ending term
    (e.g. "print(") immediately followed by its own argument
    ("print(applicant_summary)"). _compile_term_pattern() now only applies
    a boundary assertion on a side where the term's own edge character is
    itself a word character — see that function's docstring for the full
    reasoning. Confirmed via direct reproduction that this fix restores
    matching for "eval(user_input)", "cache.setex(...)",
    "print(applicant_summary)", and "session[user_id]" while the original
    #63 false-positive tests (race/mobile) still correctly reject.

    Trade-off worth knowing: this also stops matching a term as a prefix of
    a longer identifier (e.g. "mobile" no longer matches inside
    "mobile_number", since "_" counts as a word character) — acceptable
    here since the real demo repo uses the bare field name, not a suffixed
    variant, but worth remembering if trigger terms ever need prefix
    matching against a real codebase that uses such suffixes.
    """
    pattern = _COMPILED_TERM_PATTERNS.get(term)
    if pattern is None:  # a term not in SCREENER_TRIGGERS (e.g. ad-hoc test input) — compile on the fly
        pattern = _compile_term_pattern(term)
    return pattern.search(text) is not None


def scan(code_diff: str) -> ScreenerResult:
    """
    Binary trigger: scan a code diff's text for any Screener keyword.
    No AST-only fields are hit here yet (that's Diagnostician's job) — this
    is deliberately dumb and fast, string-level matching against the raw diff.
    """
    matched_buckets = []
    matched_terms = []

    for bucket, terms in SCREENER_TRIGGERS.items():
        for term in terms:
            if _term_matches(code_diff, term):
                matched_buckets.append(bucket)
                matched_terms.append(term)

    matched_buckets = sorted(set(matched_buckets))
    return ScreenerResult(matched=bool(matched_buckets), matched_buckets=matched_buckets, matched_terms=matched_terms)


def split_diff_into_fragments(code_diff: str) -> list[dict]:
    """
    Splits a multi-file diff into independent per-file, per-function
    fragments, so a PR touching several unrelated concerns (e.g. one file
    with a bias-in-scoring issue, another with an unscoped agent tool)
    doesn't get isolated into one jumbled blob for Diagnostician to reason
    about. Real production diffs commonly touch multiple files/functions in
    one PR — this isn't a demo-only contrivance.

    Splits on `diff --git a/X b/X` for file boundaries, then on top-level
    `def `/`class ` lines for function boundaries within a file (unified
    diffs for brand-new files carry the whole file as one hunk with no
    function-name context, so hunk-header splitting alone isn't enough
    here — this is deliberately closer to a lightweight AST-aware split
    than pure hunk splitting).

    Returns a list of {"file": str, "text": str} fragments. Falls back to
    treating the whole diff as one fragment (file="<diff>") if no
    `diff --git` markers are found (e.g. a pre-split test payload).
    """
    file_pattern = re.compile(r"^diff --git a/(\S+) b/\S+", re.MULTILINE)
    file_matches = list(file_pattern.finditer(code_diff))
    if not file_matches:
        return [{"file": "<diff>", "text": code_diff}]

    file_chunks = []
    for idx, m in enumerate(file_matches):
        start = m.start()
        end = file_matches[idx + 1].start() if idx + 1 < len(file_matches) else len(code_diff)
        file_chunks.append({"file": m.group(1), "text": code_diff[start:end]})

    fragments = []
    # finding #64: this used to allow arbitrary leading whitespace after the
    # "+", so an indented class method counted as a boundary too, contrary
    # to the docstring's "top-level def/class" claim — a class's __init__
    # (building a scoring table) and its score() method could get split
    # into separate fragments, dropping context ALBP-001's strict evidence
    # bar needs. Requiring the "+" to be immediately followed by def/class
    # (no whitespace) restricts this to genuinely top-level definitions.
    func_pattern = re.compile(r"^\+(?:def|class)\s+\w+")
    for chunk in file_chunks:
        lines = chunk["text"].splitlines()
        boundaries = [i for i, line in enumerate(lines) if func_pattern.match(line)]
        if len(boundaries) <= 1:
            fragments.append(chunk)  # nothing to split further
            continue
        # finding #5: this used to have an end sentinel (len(lines)) but no
        # start sentinel, so lines[0:boundaries[0]] — imports, module-level
        # constants, diff/hunk headers, any added top-level code before the
        # first def/class — was silently dropped from every fragment. A
        # fragment matched by Screener's whole-diff precheck but containing
        # none of the actual triggering line would reach Diagnostician
        # effectively un-diagnosed.
        if boundaries[0] != 0:
            boundaries = [0] + boundaries
        boundaries.append(len(lines))
        for i in range(len(boundaries) - 1):
            fragments.append({"file": chunk["file"], "text": "\n".join(lines[boundaries[i]:boundaries[i + 1]])})

    return fragments


def isolate_fragment(code_diff: str, matched_terms: list, context_lines: int = 3) -> str:
    """
    Given a diff and the terms that matched, return just the surrounding
    lines (not the whole file) to hand to Diagnostician — keeps Tier 2's
    context window small and its reasoning focused.
    """
    lines = code_diff.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        # finding #33: now shares _term_matches with scan() instead of its
        # own separate substring check, so the two can't silently disagree
        # about what counts as a match.
        if any(_term_matches(line, term) for term in matched_terms):
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                keep.add(j)
    return "\n".join(lines[i] for i in sorted(keep))


if __name__ == "__main__":
    # quick smoke test against a hardcoded "bad" and "clean" sample —
    # real test suite goes in tests/, this is just a sanity check while building
    bad_sample = '''
import openai

def get_underwriting_opinion(client):
    prompt = f"Applicant {client.first_name} {client.last_name}, DOB {client.date_of_birth}, " \\
             f"income {client.annual_income}, blood group {client.blood_group}. Should we approve?"
    response = openai.chat.completions.create(model="gpt-4", messages=[{"role": "user", "content": prompt}])
    return response
'''

    clean_sample = '''
def calculate_monthly_payment(principal, rate, months):
    r = rate / 12 / 100
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)
'''

    for label, sample in [("BAD sample", bad_sample), ("CLEAN sample", clean_sample)]:
        result = scan(sample)
        print(f"--- {label} ---")
        print(result.to_dict())
        if result.matched:
            print("isolated fragment:")
            print(isolate_fragment(sample, result.matched_terms))
        print()
