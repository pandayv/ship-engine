"""
Relay's read layer. Aggregates already-stored findings into the shapes
its tools return — and nothing else.

THE LATENCY BUDGET IS THE ARCHITECTURE. Alexa+ requires a round-trip
under 500 milliseconds. A real diagnosis takes roughly three seconds on
Nova Lite, ten times the entire budget, so Relay can never trigger one.
Everything here reads findings Detector and Triage already produced and
stored; nothing in this module calls a model, fetches a diff, or reaches
GitHub. That is not a simplification for now — it is the only shape that
fits, and it is why Relay genuinely adds no judgment of its own.

Measured against the live endpoint on 2026-09-24: ~230 ms warm, ~2.3 s
cold. Warm leaves roughly 270 ms of headroom for the read plus protocol
overhead; cold blows the budget outright, which is why the deployment
needs a keep-warm schedule rather than relying on traffic.

On the storage access pattern: these aggregates currently come from a
filtered DynamoDB Scan, because alert_id is the table's only key. At the
scale this runs at (tens of alerts) a scan costs single-digit
milliseconds and is comfortably inside the budget. It will not stay that
way — a scan is O(table), so this becomes the first thing to break as
connected repositories grow. The fix when that day comes is a secondary
index keyed on repo, not a cache here. Written down rather than
pre-optimised: the measurement says there is no problem yet, and guessing
at one would add a moving part for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.relay.modality import Spoken
from src.storage.alert_store import SEVERITY_BLOCKING, Alert, list_active_alerts
from src.storage.status_store import read_summary


@dataclass(frozen=True)
class PullRequestFindings:
    repo: str
    pr_number: int
    blocking: int
    review: int
    findings: list[Alert] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.blocking + self.review

    @property
    def label(self) -> str:
        return f"{self.repo}#{self.pr_number}"


@dataclass(frozen=True)
class ReleaseStatus:
    """What voice is allowed to know: counts, and where to look next."""

    pull_requests: list[PullRequestFindings]

    @property
    def blocking(self) -> int:
        return sum(p.blocking for p in self.pull_requests)

    @property
    def review(self) -> int:
        return sum(p.review for p in self.pull_requests)

    @property
    def total(self) -> int:
        return self.blocking + self.review

    @property
    def most_urgent(self) -> PullRequestFindings | None:
        """The PR a person should look at first: blocking findings win over
        volume, because one stopped merge matters more than three advisory
        flags, and ties break toward the PR carrying more of them."""
        if not self.pull_requests:
            return None
        return max(self.pull_requests, key=lambda p: (p.blocking, p.total))

    def to_spoken(self, screen_hint: str | None = None) -> Spoken:
        """One sentence. Never a list — see src/relay/modality.py.

        Deliberately does not name files, taxonomy ids, citations or risk
        scores, or a specific PR/repo label. Those are screen facts. Voice
        states one actionable fact (how many, blocking or not, how many
        places) and always ends with what to do next — never a bare status
        with no implied action.

        The middle band (REVIEW, non-blocking) is a real, equally
        dismissable Gate alert, exactly like a blocking one — the only
        difference is whether the merge is stopped meanwhile. An earlier
        version of this sentence said "N blocking findings and M flagged
        for review", which reads as if "flagged for review" is a separate,
        lesser category the blocking ones aren't also part of. They are.
        When there are blockers, only the blocking count is spoken; the
        non-blocking ones are still real and still on the screen, just not
        headline material when something is already actively stopping the
        merge.
        """
        if self.total == 0:
            return Spoken("No blocking issues. Good to move ahead.")

        multi_pr = len(self.pull_requests) > 1

        if self.blocking:
            noun = "blocker" if self.blocking == 1 else "blockers"
            sentence = f"{self.blocking} {noun} found"
            if multi_pr:
                sentence += f" across {len(self.pull_requests)} PRs"
            sentence += ". Ready for your decision."
        else:
            noun = "issue" if self.review == 1 else "issues"
            sentence = f"{self.review} {noun} found"
            if multi_pr:
                sentence += f" across {len(self.pull_requests)} PRs"
            sentence += ". None blocking. Ready for your review."

        return Spoken(sentence, screen_hint=screen_hint)


def _group(alerts: list[Alert]) -> list[PullRequestFindings]:
    buckets: dict[tuple[str, int], list[Alert]] = {}
    for alert in alerts:
        buckets.setdefault((alert.repo, alert.pr_number), []).append(alert)

    grouped = [
        PullRequestFindings(
            repo=repo,
            pr_number=pr,
            blocking=sum(1 for a in items if a.severity == SEVERITY_BLOCKING),
            review=sum(1 for a in items if a.severity != SEVERITY_BLOCKING),
            findings=sorted(items, key=lambda a: (-a.risk_score, a.file)),
        )
        for (repo, pr), items in buckets.items()
    ]
    # Oldest first: a finding that has been sitting unresolved longest is
    # the one most likely to be holding somebody up.
    grouped.sort(key=lambda p: min(a.created_at for a in p.findings))
    return grouped


def release_status() -> ReleaseStatus:
    """The voice path. Reads the pre-computed summary — one GetItem, no
    aggregation — so response time is independent of how many findings
    exist. See src/storage/status_store.py for why the aggregation happens
    on the write side instead."""
    summary = read_summary()
    return ReleaseStatus(pull_requests=[
        PullRequestFindings(repo=c.repo, pr_number=c.pr_number,
                            blocking=c.blocking, review=c.review, findings=[])
        for c in summary.pull_requests
    ])


def findings_detail() -> ReleaseStatus:
    """The screen path. Reads the alerts themselves, because a screen needs
    the detail the summary deliberately does not carry."""
    return ReleaseStatus(pull_requests=_group(list_active_alerts()))
