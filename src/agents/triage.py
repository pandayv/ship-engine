"""
Triage routes the build based on Detector's risk score. Pure conditional logic, no judgment of
its own, no AWS dependency (beyond importing src.taxonomy and
src.agents.detector.DetectorOutput, neither of which pulls in strands/numpy/boto3).

The freeze threshold is looked up per taxonomy_id from src/taxonomy.py's REGISTRY, which
explains each category's value. The FALLBACK_* constants below apply only to a taxonomy_id the
registry does not recognize. That should not happen (see tests/test_taxonomy_consistency.py),
but a conservative default is safer than a crash in routing logic meant to be simple and
reliable.

Three routing bands. A finding the model confirmed but scored below the blocking bar creates a
visible alert for a human WITHOUT blocking the merge. Only findings at or above the
per-category blocking threshold stop anything. See src/taxonomy.py for the band definitions
and for a note on severity being used as a proxy for confidence.

route() takes Detector's DetectorOutput directly: one schema, and route() gets DetectorOutput's
"matched implies populated" validation for free instead of trusting every caller to construct
a valid shape.
"""

from dataclasses import dataclass
from enum import Enum

from src.agents.detector import DetectorOutput
from src.taxonomy import BLOCK_THRESHOLDS, REVIEW_THRESHOLDS


class BuildAction(str, Enum):
    PASS = "pass"
    LOG_WARNING = "log_warning"
    REVIEW = "review"
    FREEZE = "freeze"


# Whether each action stops the build. Kept as an explicit mapping rather
# than an `action == FREEZE` check scattered across callers: REVIEW is the
# whole point of the middle band and the one most likely to be mistaken for
# blocking by a future reader (it creates an alert, exactly like FREEZE
# does — the difference is only whether the merge is stopped).
BLOCKING_ACTIONS = frozenset({BuildAction.FREEZE})

# Whether each action creates a persisted alert a human is asked to look at.
ALERTING_ACTIONS = frozenset({BuildAction.FREEZE, BuildAction.REVIEW})

FALLBACK_BLOCK_THRESHOLD = 7.5  # fallback only — see module docstring
FALLBACK_REVIEW_THRESHOLD = 5.0


@dataclass
class TriageDecision:
    action: BuildAction
    reason: str

    @property
    def blocks_build(self) -> bool:
        return self.action in BLOCKING_ACTIONS

    @property
    def creates_alert(self) -> bool:
        return self.action in ALERTING_ACTIONS


def route(verdict: DetectorOutput) -> TriageDecision:
    if not verdict.matched:
        return TriageDecision(action=BuildAction.PASS, reason="No violation detected.")

    if verdict.risk_score is None:
        raise ValueError("Detector matched a violation but returned no risk_score — cannot route.")

    block_at = BLOCK_THRESHOLDS.get(verdict.taxonomy_id, FALLBACK_BLOCK_THRESHOLD)
    review_at = REVIEW_THRESHOLDS.get(verdict.taxonomy_id, FALLBACK_REVIEW_THRESHOLD)

    if verdict.risk_score >= block_at:
        return TriageDecision(
            action=BuildAction.FREEZE,
            reason=f"{verdict.taxonomy_id} scored {verdict.risk_score} (>= {block_at}). "
                   f"Build frozen pending Gate review.",
        )

    if verdict.risk_score >= review_at:
        return TriageDecision(
            action=BuildAction.REVIEW,
            reason=f"{verdict.taxonomy_id} scored {verdict.risk_score} "
                   f"(>= {review_at}, below the {block_at} blocking bar). "
                   f"Flagged for human review; build continues.",
        )

    return TriageDecision(
        action=BuildAction.LOG_WARNING,
        reason=f"{verdict.taxonomy_id} scored {verdict.risk_score} (< {review_at}). "
               f"Logged, build continues.",
    )


if __name__ == "__main__":
    clean = DetectorOutput(matched=False)
    low = DetectorOutput(matched=True, taxonomy_id="PIIE-001", risk_score=4.0,
                               plain_english_summary="Applicant email is stored but never sent externally.",
                               citation="GDPR Art. 32(1)(a)", remediation_patch="")
    middling = DetectorOutput(matched=True, taxonomy_id="PIIE-001", risk_score=6.0,
                                    plain_english_summary="Applicant email reaches a third-party analytics call.",
                                    citation="GDPR Art. 32(1)(a)", remediation_patch="")
    high = DetectorOutput(matched=True, taxonomy_id="PIIE-001", risk_score=9.2,
                                plain_english_summary="Raw applicant PII sent to an external LLM.",
                                citation="GDPR Art. 32(1)(a)", remediation_patch="")

    for label, v in [("clean", clean), ("low", low), ("middling", middling), ("high", high)]:
        decision = route(v)
        print(f"{label}: {decision.action.value} — {decision.reason}")
