"""
Healing Loop is a standalone, periodic corpus-grounding check — see
scripts/healing_loop.py's own docstring for why it's deliberately NOT
wired into Detector's per-fragment hot path. These tests are pure-logic:
no real network call, no AWS credentials, a synthetic corpus file rather
than depending on the real rag_corpus/ text staying exactly as it is
today.
"""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import healing_loop  # noqa: E402

SAMPLE_FILE = """Sample Regulation — Widget safety
Source: The Widget Safety Act
Retrieved verbatim from https://example.test/widget-act on 2026-09-03.

Widgets must be manufactured with a red safety cap.

Note: this is our own commentary, not part of the statute text."""


@pytest.fixture
def corpus_file(tmp_path):
    path = tmp_path / "widget_act.txt"
    path.write_text(SAMPLE_FILE, encoding="utf-8")
    return path


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise requests.HTTPError(f"{self._status}")


def test_source_url_extracts_the_recorded_url(corpus_file):
    assert healing_loop.source_url(corpus_file) == "https://example.test/widget-act"


def test_source_url_none_when_header_has_no_recognizable_line(tmp_path):
    path = tmp_path / "malformed.txt"
    path.write_text("Title only, no source header\n\nBody text.", encoding="utf-8")
    assert healing_loop.source_url(path) is None


def test_strip_html_removes_tags_scripts_and_styles():
    html = "<html><head><style>.x{}</style><script>evil()</script></head><body><p>Real text</p></body></html>"
    assert healing_loop.strip_html(html) == "Real text"


def test_normalize_treats_curly_and_straight_quotes_as_equivalent():
    assert healing_loop.normalize("controller’s data") == healing_loop.normalize("controller's data")


def test_normalize_collapses_whitespace():
    assert healing_loop.normalize("a   b\n\nc") == "a b c"


def test_normalize_strips_a_leading_numbered_marker():
    assert healing_loop.normalize("1. Taking into account the state of the art") \
        == "Taking into account the state of the art"


def test_normalize_strips_inline_lettered_sub_item_markers_after_clause_boundaries():
    # GDPR Art. 32's four sub-items are one chunk in the stored corpus:
    # "...appropriate: (a) ...; (b) ...; (c) ..." — none of a/b/c/d appear
    # as literal text on the live page (CSS/<ol> auto-numbering), so all
    # three markers need stripping, not just the first.
    stored = "appropriate: (a) the pseudonymisation; (b) the ability; (c) the availability"
    assert healing_loop.normalize(stored) == "appropriate: the pseudonymisation; the ability; the availability"


def test_normalize_leaves_a_parenthetical_alone_when_not_at_a_clause_boundary():
    # Must not strip real content just because it happens to look like a
    # marker — only a marker sitting right at a clause boundary is safe
    # to treat as pure enumeration noise.
    assert healing_loop.normalize("the risk (low) is acceptable") == "the risk (low) is acceptable"


def test_normalize_strips_consecutive_markers():
    assert healing_loop.normalize("2. (a) the first sub-item") == "the first sub-item"


def test_check_file_grounded_when_the_live_page_still_has_the_text(monkeypatch, corpus_file):
    live_html = "<html><body><p>Widgets must be manufactured with a red safety cap.</p></body></html>"
    monkeypatch.setattr(healing_loop.requests, "get", lambda *a, **k: _FakeResponse(live_html))

    result = healing_loop.check_file(corpus_file)

    assert result["status"] == "grounded"
    assert result["chunks_checked"] == 1
    # The "Note:" paragraph must never be checked against the live page —
    # it's our own commentary, chunk_file() already excludes it.
    assert result["url"] == "https://example.test/widget-act"


def test_check_file_flags_drift_when_the_wording_changed(monkeypatch, corpus_file):
    live_html = "<html><body><p>Widgets must be manufactured with a BLUE safety cap.</p></body></html>"
    monkeypatch.setattr(healing_loop.requests, "get", lambda *a, **k: _FakeResponse(live_html))

    result = healing_loop.check_file(corpus_file)

    assert result["status"] == "drift_detected"
    assert result["drifted_paragraphs"] == [0]


def test_check_file_ignores_curly_quote_and_whitespace_noise(monkeypatch, corpus_file):
    # A page re-render that swaps straight quotes for curly ones, or wraps
    # differently, is not a wording change — must not register as drift.
    live_html = "<html><body><p>Widgets   must be manufactured\nwith a red safety cap.</p></body></html>"
    monkeypatch.setattr(healing_loop.requests, "get", lambda *a, **k: _FakeResponse(live_html))

    result = healing_loop.check_file(corpus_file)

    assert result["status"] == "grounded"


def test_check_file_reports_fetch_error_distinctly_from_drift(monkeypatch, corpus_file):
    def _raise(*a, **k):
        raise requests.ConnectionError("network is down")

    monkeypatch.setattr(healing_loop.requests, "get", _raise)

    result = healing_loop.check_file(corpus_file)

    assert result["status"] == "fetch_error"
    assert "network is down" in result["error"]


def test_check_file_reports_http_error_status_as_fetch_error(monkeypatch, corpus_file):
    monkeypatch.setattr(healing_loop.requests, "get", lambda *a, **k: _FakeResponse("", status=404))

    result = healing_loop.check_file(corpus_file)

    assert result["status"] == "fetch_error"


def test_check_file_no_source_url_short_circuits_without_a_network_call(monkeypatch, tmp_path):
    path = tmp_path / "malformed.txt"
    path.write_text("Title only\n\nBody.", encoding="utf-8")
    calls = []
    monkeypatch.setattr(healing_loop.requests, "get", lambda *a, **k: calls.append(1))

    result = healing_loop.check_file(path)

    assert result["status"] == "no_source_url"
    assert calls == []


def test_main_returns_zero_when_everything_is_grounded(monkeypatch):
    monkeypatch.setattr(healing_loop, "CORPUS_DIR", Path("unused"))
    monkeypatch.setattr(
        healing_loop, "check_file",
        lambda p: {"file": "a", "status": "grounded", "chunks_checked": 1, "url": "https://example.test/a"},
    )
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: [Path("a.txt")])
    assert healing_loop.main() == 0


def test_main_returns_nonzero_when_a_file_drifted(monkeypatch):
    monkeypatch.setattr(healing_loop, "CORPUS_DIR", Path("unused"))
    monkeypatch.setattr(
        healing_loop, "check_file",
        lambda p: {"file": "a", "status": "drift_detected", "chunks_checked": 1, "drifted_paragraphs": [0], "url": "u"},
    )
    monkeypatch.setattr(Path, "rglob", lambda self, pattern: [Path("a.txt")])
    assert healing_loop.main() == 1
