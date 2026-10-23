"""
The alert_id is derived from what identifies a violation. Deriving it from only
(repo, pr_number, file, taxonomy_id) is too coarse: two different violations of the same
taxonomy_id in the same file (an SSN leak in one function, a separate account-number leak in
another, both PIIE-001) would hash to the same alert_id. The second put_alert() would silently
overwrite the first violation's row with no record it ever existed, and after resolution the same
collision would block any future, genuinely new violation of that taxonomy_id/file combination
from ever being persisted. The isolated fragment's text is part of the key to prevent that.
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
    # Re-detecting the identical fragment (a webhook redelivery, a Lambda async retry) must
    # derive the identical id, so the retry overwrites the same row instead of creating a
    # duplicate.
    first = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="logger.info(f'SSN: {applicant.ssn}')",
    )
    retry = _deterministic_alert_id(
        repo="pandayv/micro-finance", pr_number=1, file="loans/apply.py", taxonomy_id="PIIE-001",
        fragment_text="logger.info(f'SSN: {applicant.ssn}')",
    )
    assert first == retry
