"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _never_send_a_real_alexa_alert(monkeypatch):
    """The pipeline sends an Alexa notification when it records a finding. Developer
    machines have real AWS credentials, so without this a test that runs the pipeline
    could read the real users table and notify a real Echo. tests/test_alexa_notify.py
    tests the notifier itself, with fakes, by calling it directly."""
    monkeypatch.setattr("src.api.main.notify_new_finding", lambda *args, **kwargs: 0)


@pytest.fixture(autouse=True)
def _never_read_the_real_push_tables(monkeypatch):
    """Developer machines have real AWS credentials and the push tables hold real screen
    registrations. A test that forgets to fake them would read those and pass or fail
    depending on whose laptop is registered. Tests that need the tables fake them
    themselves (tests/test_push_flow.py)."""
    def refuse():
        raise RuntimeError("tests must not reach the real push tables")
    monkeypatch.setattr("src.storage.push_store._subscriptions", refuse)
    monkeypatch.setattr("src.storage.push_store._pending", refuse)
