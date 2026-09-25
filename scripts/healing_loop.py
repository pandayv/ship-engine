#!/usr/bin/env python3
"""
Healing Loop — periodically verifies the RAG corpus is still grounded in
its source, so a citation Detector hands to a human is never quietly
resting on regulation text that has since been amended.

Deliberately NOT wired into Detector's per-fragment hot path. SHIP is
built to run against dozens of connected repos and hundreds of PRs — a
network fetch to four external regulation sites on every single fragment
review would be real, avoidable latency and cost multiplied by every PR
SHIP ever reviews, for a question ("has the source text changed since we
sourced it?") whose answer only ever changes when a regulator actually
amends something, not once per PR. This runs on its own schedule instead
(cron, EventBridge — whatever's available; nothing here assumes AWS) and
touches nothing Detector reads at request time.

What it checks: each rag_corpus/**/*.txt file's header records exactly
where its text was sourced ("Retrieved verbatim from <url> on <date>.").
This script re-fetches that url and confirms every body paragraph
src/rag/chunker.py would actually turn into a citable chunk is still
present, verbatim, in the live page — reusing chunk_file() directly
rather than re-implementing the header/Note-paragraph filtering, so this
checks exactly what Detector can actually retrieve and cite, nothing
more and nothing less.

What "drift" means here: a paragraph that no longer appears verbatim at
its source. That's flagged for a human to re-source, not auto-corrected —
silently rewriting a legal citation from an automated text diff is
exactly the kind of unverified action this whole project's "trust, but
verify" design principle exists to prevent. This script raises the flag;
a human still updates the corpus.

Known, deliberate limitation: a plain-text containment check can't tell
"the regulation was amended" apart from "the page was redesigned around
the same text" — both look like drift here, and disambiguating them
requires the same human judgment either way. False positives are the
safe failure mode for a job whose only action is "tell a human to look."

Verified against the real live sources, not just synthetic fixtures
(2026-09-10) — found and fixed two genuinely generic false-positive
causes this way: HTML numeric entities (genai.owasp.org's "&#8211;" for
an en dash, silently mismatching the literal character in the stored
text — html.unescape() fixed this for every entity, not just that one)
and CSS/`<ol>`-rendered clause numbering (several sites never put "1."
or "(a)" in the HTML at all, including markers buried mid-paragraph, not
just at the start — normalize() now strips these at genuine clause
boundaries only). Two sources still show known false positives after
those fixes, deliberately left as documented limitations rather than
chased further: artificialintelligenceact.eu injects interactive
glossary-tooltip text inline (a term appears, then its definition, both
flattened into the plain text by any tag-stripping approach), and one
OWASP heading has a trailing colon in the stored corpus that the live
page doesn't render — a transcription-formatting difference, not an
extraction bug, and not worth a corpus edit that would need a fresh
embedding regen for one punctuation mark. Both would need site-specific
scraping logic to fully resolve, which would trade away the whole point
of staying a generic, lightweight check for one or two sites' quirks.
"""

import html
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

from src.rag.chunker import chunk_file  # noqa: E402

CORPUS_DIR = REPO_ROOT / "rag_corpus"
REQUEST_TIMEOUT = 15
USER_AGENT = "ship-healing-loop/1.0 (+https://github.com/pandayv/ship-engine)"

SOURCE_URL_RE = re.compile(r"Retrieved verbatim from (\S+?) on \d{4}-\d{2}-\d{2}\.")


def source_url(path: Path) -> str | None:
    """The url a corpus file's own header says it was sourced from — the
    header is always the file's first blank-line-delimited paragraph, same
    boundary chunk_file() itself splits on."""
    header = path.read_text(encoding="utf-8").split("\n\n", 1)[0]
    match = SOURCE_URL_RE.search(header)
    return match.group(1) if match else None


def strip_html(page: str) -> str:
    """Same lightweight approach used to source this corpus in the first
    place (curl + regex — see rag_corpus file headers) rather than a full
    HTML-parser dependency: good enough for a containment check, not
    meant to faithfully render the page.

    Real bug caught while first running this against the live sources
    (2026-09-10): a hand-rolled list of entity replacements missed
    &#8211; (en dash), which genai.owasp.org's page uses in the exact
    sentence this corpus quotes — a false "drift" positive with nothing
    to do with the actual text changing. html.unescape() decodes every
    named and numeric entity in one call; no reason to maintain a partial
    list by hand when the standard library already does this completely.
    """
    text = re.sub(r"<script[^>]*>.*?</script>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize(html.unescape(text))


_ENUM_MARKER_RE = re.compile(r"(?:^|(?<=[;:.]\s))(?:\d+\.|\(\w{1,3}\))\s+")


def normalize(text: str) -> str:
    """Re-rendering noise that must never register as drift: curly vs.
    straight quotes, en/em dashes vs. hyphens, collapsed whitespace, and
    clause-list numbering ("1. ", "(a) ", "(b) ", ...). That last one is a
    real false-positive source found while first running this live:
    several of these regulation sites render numbered/lettered sub-clauses
    as CSS/`<ol>` auto-numbering with no literal "1." or "(a)" text
    anywhere in the HTML — including markers buried mid-paragraph, not
    just at the very start (GDPR Art. 32's four sub-items are one chunk,
    "...appropriate: (a) ...; (b) ...; (c) ...", and none of a/b/c/d
    appear as text on the live page). A stored chunk built from the
    numbered original therefore never matches verbatim even when the
    actual wording hasn't changed one bit.

    Only strips a marker at a genuine clause boundary — the very start of
    the text, or right after ';', ':', or '.' followed by whitespace —
    not after an arbitrary word, so an incidental parenthetical like
    "the risk (low) is acceptable" is left alone. Stripping the numbering
    only, never the clause text itself, means a real wording change
    inside a sub-item still gets caught; only the enumeration marker
    stops being load-bearing.
    """
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip()
    # Repeated, not single-pass: consecutive markers ("2. (a) ...") and a
    # marker that becomes newly boundary-adjacent only after the previous
    # one was removed both need more than one sweep to fully clear.
    previous = None
    while previous != text:
        previous = text
        text = _ENUM_MARKER_RE.sub("", text)
    return text


def check_file(path: Path) -> dict:
    try:
        rel_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(path)  # a path outside REPO_ROOT (e.g. a test fixture) — display it as-is
    url = source_url(path)
    if url is None:
        return {"file": rel_path, "status": "no_source_url", "chunks_checked": 0}

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
    except requests.RequestException as e:
        # Unreachable is not the same claim as "drifted" — a transient
        # network blip or a site that started blocking bots shouldn't read
        # as "the law changed." Surfaced separately so a human can tell
        # the two apart at a glance.
        return {"file": rel_path, "url": url, "status": "fetch_error", "error": str(e), "chunks_checked": 0}

    live_text = strip_html(response.text)
    chunks = chunk_file(path)
    drifted = [c.paragraph_index for c in chunks if normalize(c.text) not in live_text]

    return {
        "file": rel_path,
        "url": url,
        "status": "drift_detected" if drifted else "grounded",
        "chunks_checked": len(chunks),
        "drifted_paragraphs": drifted,
    }


def main() -> int:
    results = [check_file(p) for p in sorted(CORPUS_DIR.rglob("*.txt"))]

    for r in results:
        if r["status"] == "grounded":
            print(f"  grounded    {r['file']} ({r['chunks_checked']} chunks, {r['url']})")
        elif r["status"] == "drift_detected":
            print(f"  DRIFT       {r['file']} — paragraph(s) {r['drifted_paragraphs']} no longer found at {r['url']}")
        elif r["status"] == "fetch_error":
            print(f"  fetch error {r['file']} — {r['error']}")
        else:
            print(f"  no source url recorded in {r['file']}")

    drifted = [r for r in results if r["status"] == "drift_detected"]
    if drifted:
        print(f"\n{len(drifted)} file(s) need a human to re-source them — see DRIFT lines above.")
        return 1
    print("\nAll sourced regulation text still verbatim-matches its source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
