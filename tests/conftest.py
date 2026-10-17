"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _never_send_a_real_alexa_alert(monkeypatch):
    """The pipeline sends an Alexa notification when it records a finding. Developer
    machines have real AWS credentials, so without this a test that runs the pipeline
    could read the real users table and notify a real Echo. tests/test_alexa_notify.py
    tests the notifier itself, with fakes, by calling it directly."""
    monkeypatch.setattr("src.api.main.notify_new_finding", lambda *args, **kwargs: 0)
