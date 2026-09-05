"""
Triage — routes the build based on Diagnostician's risk score.
Pure conditional logic, no judgment of its own, no AWS dependency (beyond
importing src.taxonomy, itself a plain dataclass registry with no AWS/heavy
dependency of its own — see that module's docstring).

Finding #41: the freeze threshold used to be one constant applied uniformly
across all six taxonomy IDs. It's now looked up per taxonomy_id from
src/taxonomy.py's REGISTRY (see that file for the full reasoning behind
each category's specific value) — HIGH_RISK_THRESHOLD below is kept only
as the fallback for a taxonomy_id the registry doesn't recognize, which
should not happen in practice (see tests/test_taxonomy_consistency.py) but
is safer as a conservative default than a crash in what's meant to be
simple, reliable routing logic.
"""

from dataclasses import dataclass
from enum import Enum

from src.taxonomy import RISK_THRESHOLDS


class BuildAction(str, Enum):
    PASS = "pass"
    LOG_WARNING = "log_warning"
    FREEZE = "freeze"


HIGH_RISK_THRESHOLD = 7.5  # fallback only — see module docstring


@dataclass
class DiagnosticianVerdict:
    """The shape of what Diagnostician is expected to hand Triage.
    Defined here so Triage can be built/tested before Diagnostician exists."""
    matched: bool
    taxonomy_id: str | None = None
    risk_score: float | None = None
    plain_english_summary: str | None = None
    citation: str | None = None
    remediation_patch: str | None = None


@dataclass
class TriageDecision:
    action: BuildAction
    reason: str


def route(verdict: DiagnosticianVerdict) -> TriageDecision:
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
    clean = DiagnosticianVerdict(matched=False)
    low = DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=4.0)
    high = DiagnosticianVerdict(matched=True, taxonomy_id="PIIE-001", risk_score=9.2,
                                 plain_english_summary="Raw applicant PII sent to an external LLM.",
                                 citation="GDPR Art. 32(1)(a)")

    for label, v in [("clean", clean), ("low", low), ("high", high)]:
        decision = route(v)
        print(f"{label}: {decision.action.value} — {decision.reason}")
