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
# imports/egress/pii_data -> PIIE (Detector #1, must-ship)
# bias_data                -> ALBP (Detector #2 candidate A)
# agentic                  -> TLGP-001 (not currently a detector, kept for later)
SCREENER_TRIGGERS = {
    "imports": ["openai", "anthropic", "langchain", "llamaindex", "transformers", "autogen"],
    "egress": ["httpx.post", "requests.post", "client.chat.completions", "axios.post"],
    "pii_data": ["email", "ssn", "tax_id", "credit_card", "biometric", "phone_number",
                 # fintech-flavored additions per the MicroPyramid demo repo's actual
                 # Client model fields (blood_group, dob, mobile, pincode-as-PII context)
                 "blood_group", "date_of_birth", "mobile", "annual_income", "account_number"],
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
            # word-boundary-ish match, case-insensitive, tolerant of the
            # trailing "(" already present in some agentic trigger terms
            pattern = re.escape(term)
            if re.search(pattern, code_diff, flags=re.IGNORECASE):
                matched_buckets.append(bucket)
                matched_terms.append(term)

    matched_buckets = sorted(set(matched_buckets))
    return ScreenerResult(matched=bool(matched_buckets), matched_buckets=matched_buckets, matched_terms=matched_terms)


def isolate_fragment(code_diff: str, matched_terms: list, context_lines: int = 3) -> str:
    """
    Given a diff and the terms that matched, return just the surrounding
    lines (not the whole file) to hand to Diagnostician — keeps Tier 2's
    context window small and its reasoning focused.
    """
    lines = code_diff.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if any(term.lower() in line.lower() for term in matched_terms):
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
