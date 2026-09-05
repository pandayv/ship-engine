"""
Triage — routes the build based on Diagnostician's risk score.
Pure conditional logic, no judgment of its own, no AWS dependency (beyond
importing src.taxonomy and src.agents.diagnostician.DiagnosticianOutput,
neither of which pull in strands/numpy/boto3 — see their own docstrings
for why those heavier imports are deliberately lazy elsewhere).

Finding #41: the freeze threshold used to be one constant applied uniformly
across all six taxonomy IDs. It's now looked up per taxonomy_id from
src/taxonomy.py's REGISTRY (see that file for the full reasoning behind
each category's specific value) — HIGH_RISK_THRESHOLD below is kept only
as the fallback for a taxonomy_id the registry doesn't recognize, which
should not happen in practice (see tests/test_taxonomy_consistency.py) but
is safer as a conservative default than a crash in what's meant to be
simple, reliable routing logic.

Finding #24: this module used to define its own DiagnosticianVerdict
dataclass, a hand-copied, field-by-field duplicate of Diagnostician's real
DiagnosticianOutput (same six fields, same order) — the docstring's
original justification ("so Triage can be tested before Diagnostician
exists") no longer applies now that Diagnostician has existed for a while.
route() now takes the real DiagnosticianOutput directly: one schema, one
place fields can drift, and route() gets DiagnosticianOutput's own
"matched implies populated" pydantic validation (finding #46) for free
instead of trusting every caller to construct a valid shape.
"""

from dataclasses import dataclass
from enum import Enum

from src.agents.diagnostician import DiagnosticianOutput
from src.taxonomy import RISK_THRESHOLDS


class BuildAction(str, Enum):
    PASS = "pass"
    LOG_WARNING = "log_warning"
    FREEZE = "freeze"


HIGH_RISK_THRESHOLD = 7.5  # fallback only — see module docstring


@dataclass
class TriageDecision:
    action: BuildAction
    reason: str


def route(verdict: DiagnosticianOutput) -> TriageDecision:
    if not verdict.matched:
        return TriageDecision(action=BuildAction.PASS, reason="No violation detected.")

    if verdict.risk_score is None:
        raise ValueError("Diagnostician matched a violation but returned no risk_score — cannot route.")

    threshold = RISK_THRESHOLDS.get(verdict.taxonomy_id, HIGH_RISK_THRESHOLD)

    if verdict.risk_score >= threshold:
        return TriageDecision(
            action=BuildAction.FREEZE,
            reason=f"{verdict.taxonomy_id} scored {verdict.risk_score} (>= {threshold}). "
                   f"Build frozen pending Attending review.",
        )

    return TriageDecision(
        action=BuildAction.LOG_WARNING,
        reason=f"{verdict.taxonomy_id} scored {verdict.risk_score} (< {threshold}). "
               f"Logged, build continues.",
    )


if __name__ == "__main__":
    clean = DiagnosticianOutput(matched=False)
    low = DiagnosticianOutput(matched=True, taxonomy_id="PIIE-001", risk_score=4.0,
                               plain_english_summary="Applicant email is stored but never sent externally.",
                               citation="GDPR Art. 32(1)(a)", remediation_patch="")
    high = DiagnosticianOutput(matched=True, taxonomy_id="PIIE-001", risk_score=9.2,
                                plain_english_summary="Raw applicant PII sent to an external LLM.",
                                citation="GDPR Art. 32(1)(a)", remediation_patch="")

    for label, v in [("clean", clean), ("low", low), ("high", high)]:
        decision = route(v)
        print(f"{label}: {decision.action.value} — {decision.reason}")
