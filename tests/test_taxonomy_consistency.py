"""
Automated consistency checks between src/taxonomy.py's REGISTRY and the
three things that must actually agree with it: Screener's trigger buckets,
Diagnostician's prompt text (both the in-process copy and the deployed
AgentCore copy), and the RAG corpus files on disk (both trees).

This is the test that should have caught finding #29 (the deployed
AgentCore Diagnostician silently drifting to a stale, PIIE-001-only
prompt/corpus for a full day) the moment it happened, instead of a manual
review catching it a day later. If this test suite is green, that specific
failure class cannot recur silently.

The deployed copy's source is read as plain text, not imported — it lives
in a separate package tree (shipagentcore/app/ship_diagnostician/) with its
own dependencies (bedrock_agentcore, a relative `from rag...` import that
only resolves inside that package's own root) that aren't installed in
this project's main venv, and don't need to be: a substring check against
the actual deployed prompt text is exactly what's needed here, and it's
more robust than importing, since it can't be fooled by import-time
mocking hiding the very drift we're checking for.
"""

from pathlib import Path

from src.taxonomy import REGISTRY

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CORPUS_DIR = REPO_ROOT / "rag_corpus"
DEPLOYED_CORPUS_DIR = REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag_corpus"
MAIN_DIAGNOSTICIAN_SOURCE = (REPO_ROOT / "src" / "agents" / "diagnostician.py").read_text()
DEPLOYED_DIAGNOSTICIAN_SOURCE = (REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "main.py").read_text()


def test_every_registry_entry_has_grounding_in_the_main_corpus():
    for entry in REGISTRY:
        for corpus_file in entry.corpus_files:
            path = MAIN_CORPUS_DIR / corpus_file
            assert path.is_file(), f"{entry.taxonomy_id} requires {corpus_file}, missing from {MAIN_CORPUS_DIR}"


def test_every_registry_entry_has_grounding_in_the_deployed_corpus():
    # This exact check — run against the DEPLOYED copy specifically — is
    # what would have caught finding #29 automatically.
    for entry in REGISTRY:
        for corpus_file in entry.corpus_files:
            path = DEPLOYED_CORPUS_DIR / corpus_file
            assert path.is_file(), (
                f"{entry.taxonomy_id} requires {corpus_file}, missing from the DEPLOYED corpus "
                f"({DEPLOYED_CORPUS_DIR}) — the live AgentCore runtime cannot ground this ID."
            )


def test_every_registry_taxonomy_id_is_mentioned_in_the_main_prompt():
    for entry in REGISTRY:
        assert entry.taxonomy_id in MAIN_DIAGNOSTICIAN_SOURCE, (
            f"{entry.taxonomy_id} is in the taxonomy registry but not mentioned anywhere in "
            f"src/agents/diagnostician.py's source (prompt or schema)"
        )


def test_every_registry_taxonomy_id_is_mentioned_in_the_deployed_prompt():
    # This is the specific check that would have caught #29: the deployed
    # copy's prompt had silently stopped mentioning 5 of 6 taxonomy IDs.
    for entry in REGISTRY:
        assert entry.taxonomy_id in DEPLOYED_DIAGNOSTICIAN_SOURCE, (
            f"{entry.taxonomy_id} is in the taxonomy registry but not mentioned anywhere in the "
            f"DEPLOYED shipagentcore/app/ship_diagnostician/main.py source — the live runtime "
            f"cannot detect this ID even if Screener flags a fragment for it."
        )


def test_every_registry_screener_bucket_is_a_real_screener_bucket():
    from src.agents.screener import SCREENER_TRIGGERS

    for entry in REGISTRY:
        for bucket in entry.screener_buckets:
            assert bucket in SCREENER_TRIGGERS, (
                f"{entry.taxonomy_id} references Screener bucket {bucket!r}, which doesn't exist "
                f"in SCREENER_TRIGGERS — this ID can never actually be escalated to Diagnostician."
            )


def test_every_registry_block_threshold_is_mentioned_in_the_main_prompt():
    # Finding #41: per-category thresholds only matter if the model's own
    # calibration (what it's told "severe" means per category) actually
    # matches what Triage enforces. A future retune of one without the
    # other would silently reintroduce the exact "model calibrates
    # against a number Triage doesn't use" problem #41 was about.
    for entry in REGISTRY:
        threshold_text = f">= {entry.block_threshold}"
        assert threshold_text in MAIN_DIAGNOSTICIAN_SOURCE, (
            f"{entry.taxonomy_id}'s threshold ({entry.block_threshold}) from src/taxonomy.py isn't "
            f"mentioned in src/agents/diagnostician.py's SYSTEM_PROMPT — the model may be calibrating "
            f"against a stale number Triage doesn't actually use."
        )


def test_every_registry_block_threshold_is_mentioned_in_the_deployed_prompt():
    for entry in REGISTRY:
        threshold_text = f">= {entry.block_threshold}"
        assert threshold_text in DEPLOYED_DIAGNOSTICIAN_SOURCE, (
            f"{entry.taxonomy_id}'s threshold ({entry.block_threshold}) from src/taxonomy.py isn't "
            f"mentioned in the DEPLOYED shipagentcore prompt — the deployed model may be calibrating "
            f"against a stale number."
        )


def test_deployed_and_main_prompts_mention_the_same_taxonomy_ids():
    # A stricter version of the two tests above: not just "every registry
    # ID is mentioned in both," but "neither prompt mentions an ID the
    # other doesn't" — catches drift in either direction, including a
    # taxonomy ID added to one copy and never ported to the other.
    for entry in REGISTRY:
        in_main = entry.taxonomy_id in MAIN_DIAGNOSTICIAN_SOURCE
        in_deployed = entry.taxonomy_id in DEPLOYED_DIAGNOSTICIAN_SOURCE
        assert in_main == in_deployed, (
            f"{entry.taxonomy_id} is mentioned in one Diagnostician copy but not the other "
            f"(main={in_main}, deployed={in_deployed}) — the two have drifted apart."
        )
