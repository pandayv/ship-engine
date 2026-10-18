"""docs/ship-sw.js: what the service worker does when the browser replaces its push subscription."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
RUNNER = Path(__file__).resolve().parent / "node" / "sw_runner.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def run(mode):
    done = subprocess.run([NODE, str(RUNNER), mode], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_replaced_subscription_is_renewed_under_the_same_key_and_open_pages_are_told():
    out = run("with-key")
    assert out["subscribeCalls"] == [{"userVisibleOnly": True, "sameKey": True}]
    assert out["posted"] == [{"type": "ship-subscription-changed", "resubscribed": True}]


def test_without_a_key_nothing_is_guessed_and_the_page_is_told_it_has_to_ask_again():
    out = run("no-key")
    assert out["subscribeCalls"] == [] and out["posted"] == [{"type": "ship-subscription-changed", "resubscribed": False}]


def test_a_refused_renewal_does_not_throw_and_is_reported():
    out = run("subscribe-fails")
    assert out["posted"] == [{"type": "ship-subscription-changed", "resubscribed": False}]
