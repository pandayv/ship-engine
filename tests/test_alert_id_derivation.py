"""
Regression test for a real bug an independent review caught (2026-09-05)
in finding #6's deterministic alert_id fix: deriving the id from only
(repo, pr_number, file, taxonomy_id) is too coarse. Two genuinely
different violations of the same taxonomy_id in the same file (e.g. an
SSN leak in one function, a separate account-number leak in another,
both PIIE-001) hashed to the identical alert_id — the second put_alert()
call would silently overwrite the first violation's row with no record it
ever existed, and after resolution, the same collision would silently
block any future, genuinely new violation of that taxonomy_id/file combo
from ever being persisted at all.
"""

from src.storage.alert_store import _deterministic_alert_id


def test_two_different_violations_same_taxonomy_and_file_get_different_ids():
    ssn_leak_id = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="logger.info(f'SSN: {applicant.ssn}')",
    )
    account_number_leak_id = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="requests.post(url, json={'account': applicant.account_number})",
    )
    assert ssn_leak_id != account_number_leak_id, (
        "two distinct violations of the same taxonomy_id in the same file must not collide on one alert_id"
    )


def test_a_real_retry_of_the_same_violation_still_produces_the_same_id():
    # finding #6's original guarantee must still hold: re-diagnosing the
    # identical fragment (a webhook redelivery, a Lambda async retry) must
    # still derive the identical id, so the retry overwrites the same row
    # instead of creating a duplicate.
    first = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="logger.info(f'SSN: {applicant.ssn}')",
    )
    retry = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="logger.info(f'SSN: {applicant.ssn}')",
    )
    assert first == retry
