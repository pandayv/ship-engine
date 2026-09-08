"""
Tests for the precomputed embedding cache.

The cache is a speed optimisation whose only real danger is silent
staleness — serving vectors for regulation text that has since changed,
which would cite law the corpus no longer contains. Most of what follows
tests refusal, not loading: the cache must decline itself whenever it
cannot prove it belongs to the corpus on disk.
"""

from pathlib import Path

import numpy as np
import pytest

from src.rag import embedding_cache
from src.rag.chunker import chunk_corpus

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIRS = (
    REPO_ROOT / "rag_corpus",
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag_corpus",
)

MODEL = "bedrock:amazon.titan-embed-text-v2:0"


def _vectors(n, dim=8):
    return np.arange(n * dim, dtype=np.float32).reshape(n, dim)


def test_round_trip_returns_the_same_vectors(tmp_path):
    texts = ["alpha", "beta", "gamma"]
    path = tmp_path / "embeddings.npz"
    embedding_cache.save(texts, MODEL, _vectors(3), path)

    loaded = embedding_cache.load(texts, MODEL, path)
    assert loaded is not None
    np.testing.assert_array_equal(loaded, _vectors(3))


def test_edited_corpus_text_invalidates_the_cache(tmp_path):
    # The dangerous case: someone rewords a regulation paragraph and does
    # not regenerate. Retrieval must not run against the old vectors.
    path = tmp_path / "embeddings.npz"
    embedding_cache.save(["alpha", "beta", "gamma"], MODEL, _vectors(3), path)

    assert embedding_cache.load(["alpha", "beta CHANGED", "gamma"], MODEL, path) is None


def test_added_chunk_invalidates_the_cache(tmp_path):
    path = tmp_path / "embeddings.npz"
    embedding_cache.save(["alpha", "beta"], MODEL, _vectors(2), path)

    assert embedding_cache.load(["alpha", "beta", "gamma"], MODEL, path) is None


def test_reordered_chunks_invalidate_the_cache(tmp_path):
    # Vectors are positional — the same texts in a different order means
    # every vector now belongs to the wrong chunk.
    path = tmp_path / "embeddings.npz"
    embedding_cache.save(["alpha", "beta"], MODEL, _vectors(2), path)

    assert embedding_cache.load(["beta", "alpha"], MODEL, path) is None


def test_different_embedding_model_invalidates_the_cache(tmp_path):
    # Titan, Gemini and Ollama embeddings are incompatible vector spaces.
    # Mixing them raises no error at all — retrieval just silently degrades
    # — so the model has to be part of the cache's identity.
    path = tmp_path / "embeddings.npz"
    embedding_cache.save(["alpha", "beta"], MODEL, _vectors(2), path)

    assert embedding_cache.load(["alpha", "beta"], "gemini:gemini-embedding-001", path) is None


def test_missing_cache_is_not_an_error(tmp_path):
    assert embedding_cache.load(["alpha"], MODEL, tmp_path / "absent.npz") is None


def test_corrupt_cache_falls_back_instead_of_crashing(tmp_path):
    # A truncated or garbage file is a reason to embed live, not a reason
    # to take down a running review.
    path = tmp_path / "embeddings.npz"
    path.write_bytes(b"this is not a numpy archive")

    assert embedding_cache.load(["alpha"], MODEL, path) is None


def test_separator_collision_cannot_forge_a_matching_fingerprint():
    # Chunk texts are hashed with an explicit length prefix rather than
    # joined on a separator, so two genuinely different chunk lists cannot
    # collide just because a separator appears inside regulation text.
    assert embedding_cache.fingerprint(["a\nb"], MODEL) != embedding_cache.fingerprint(["a", "b"], MODEL)
    assert embedding_cache.fingerprint(["ab", ""], MODEL) != embedding_cache.fingerprint(["a", "b"], MODEL)


def test_save_refuses_a_vector_count_that_does_not_match_the_chunks(tmp_path):
    with pytest.raises(ValueError, match="one to one"):
        embedding_cache.save(["alpha", "beta"], MODEL, _vectors(3), tmp_path / "embeddings.npz")


# --- drift guards against the real corpus, not fixtures ---


def test_both_corpus_copies_hold_identical_text():
    # The precompute script reuses one set of vectors for both copies, so
    # they must be identical or the mirrored cache would pass its own
    # fingerprint check while serving text the container does not have.
    # This is the same class of drift as finding #29.
    primary, mirror = ({c.text for c in chunk_corpus(d)} for d in CORPUS_DIRS)
    assert primary == mirror, (
        "rag_corpus/ and shipagentcore/app/ship_diagnostician/rag_corpus/ have diverged — "
        "reconcile them before regenerating embeddings"
    )


@pytest.mark.parametrize("corpus_dir", CORPUS_DIRS, ids=["main", "deployed"])
def test_any_committed_cache_still_matches_its_corpus(corpus_dir):
    # Not having a cache is fine — the system embeds live. Having a STALE
    # one is what this catches, and it catches it here, at development
    # time, rather than as a slow silent fallback inside a container whose
    # logs nobody is reading.
    path = embedding_cache.default_cache_path(corpus_dir)
    if not path.exists():
        pytest.skip(f"no precomputed cache at {path} — live embedding is the correct fallback")

    texts = [c.text for c in chunk_corpus(corpus_dir)]
    assert embedding_cache.load(texts, MODEL, path) is not None, (
        f"{path} no longer matches its corpus — regenerate with "
        f"scripts/precompute_embeddings.py"
    )


def test_the_two_embedding_cache_modules_are_byte_identical():
    # embedding_cache.py imports nothing project-relative, so the deployed
    # mirror can be an exact copy. Asserting that here removes a whole
    # drift surface: the two files cannot diverge unnoticed the way the
    # Diagnostician prompt copies once did.
    main = (REPO_ROOT / "src" / "rag" / "embedding_cache.py").read_bytes()
    deployed = (REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag" / "embedding_cache.py").read_bytes()
    assert main == deployed, "the deployed embedding_cache.py has drifted from src/rag/embedding_cache.py"
