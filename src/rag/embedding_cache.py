"""
Precomputed corpus embeddings, so a cold start doesn't re-embed static
regulation text through Bedrock every single time.

WHY THIS EXISTS — a real, measured production failure (2026-09-08). A live
webhook test dispatched two fragments in parallel. Each one cold-started
its own AgentCore container; each container rebuilt the whole vector store
from scratch, which is one Titan embedding call per corpus chunk; and both
bursts competed for the same account-wide 10-requests-per-minute Bedrock
quota. Adaptive retry did its job and backed both off, so one fragment
limped in at 435 seconds and the other was killed by Lambda's hard
900-second ceiling without producing anything. The corpus is STATIC between
deploys — paying for those embeddings at runtime, in every container, was
pure waste that made the system miss its deadline.

WHAT IS CACHED, AND WHAT DELIBERATELY IS NOT: only the vectors. Chunks are
always re-derived from the corpus files at load time, never deserialized
from the cache. Two reasons, both real:
  - Chunk.source_file holds an absolute path. Caching chunks would bake
    the build machine's paths (/Users/...) into citations rendered by a
    container that has no such directory.
  - Re-chunking is pure local text work, microseconds, no network. There
    is nothing to gain by caching it, and correctness to lose.

HOW STALENESS IS PREVENTED. This is the part that matters. A stale cache
is not a slow system, it is a WRONG one: it would retrieve text that is no
longer in the corpus and cite it as current law. So the artifact carries a
fingerprint over the exact chunk texts AND the embedding model id, and
load() re-derives that fingerprint from the corpus on disk before trusting
anything. Any mismatch — edited text, added article, switched backend,
different Titan version — and the cache is refused, with a loud log line,
falling back to live embedding. Falling back is correct-but-slow; using a
mismatched cache would be fast-but-wrong, which is strictly worse.

The model id is inside the fingerprint rather than merely stored beside it
because Titan v1/v2, Gemini and Ollama embeddings occupy mutually
incompatible vector spaces. Loading one backend's vectors while querying
with another produces no error at all — just silently degraded retrieval,
the exact failure mode the "Trust but verify" principle exists to catch.

See also tests/test_embedding_cache.py, which fails at development time if
a committed cache no longer matches the corpus — the same structural-guard
pattern as tests/test_taxonomy_consistency.py, which exists because a
silent drift of this kind already cost this project a full day (finding
#29).
"""

import hashlib
import json
import logging
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

CACHE_FILENAME = "embeddings.npz"


def default_cache_path(corpus_dir: Path) -> Path:
    return Path(corpus_dir) / CACHE_FILENAME


def fingerprint(chunk_texts: list[str], model_id: str) -> str:
    """
    Identity of "these exact chunks, embedded by this exact model".

    Hashes the texts with an explicit length prefix per chunk rather than
    joining on a separator: a separator can appear inside regulation text,
    which would let two genuinely different chunk lists hash identically.
    """
    digest = hashlib.sha256()
    digest.update(model_id.encode("utf-8"))
    digest.update(b"\x00")
    for text in chunk_texts:
        raw = text.encode("utf-8")
        digest.update(str(len(raw)).encode("ascii"))
        digest.update(b":")
        digest.update(raw)
    return digest.hexdigest()


def save(chunk_texts: list[str], model_id: str, vectors: np.ndarray, cache_path: Path) -> None:
    if vectors.shape[0] != len(chunk_texts):
        raise ValueError(
            f"refusing to save a cache with {vectors.shape[0]} vectors for "
            f"{len(chunk_texts)} chunks — they must correspond one to one"
        )
    meta = json.dumps({
        "fingerprint": fingerprint(chunk_texts, model_id),
        "model_id": model_id,
        "chunk_count": len(chunk_texts),
    })
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache_path, vectors=vectors.astype(np.float32), meta=np.array(meta))


def load(chunk_texts: list[str], model_id: str, cache_path: Path) -> np.ndarray | None:
    """
    Returns the cached vectors only if they provably belong to exactly
    these chunks and this model. Returns None in every other case —
    including a corrupt or unreadable file, which is a reason to embed
    live, not a reason to crash a running review.
    """
    if not cache_path.exists():
        return None

    try:
        with np.load(cache_path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"]))
            vectors = data["vectors"]
    except Exception as e:
        log.error("embedding cache at %s is unreadable (%s) — embedding live instead", cache_path, e)
        return None

    expected = fingerprint(chunk_texts, model_id)
    if meta.get("fingerprint") != expected:
        log.error(
            "embedding cache at %s does not match the current corpus or model "
            "(cache built for model %r with %s chunks; now %s chunks for model %r). "
            "Embedding live instead — regenerate with scripts/precompute_embeddings.py.",
            cache_path, meta.get("model_id"), meta.get("chunk_count"), len(chunk_texts), model_id,
        )
        return None

    if vectors.shape[0] != len(chunk_texts):
        # Belt and braces: the fingerprint already covers this, so reaching
        # here means the file was tampered with or truncated after hashing.
        log.error("embedding cache at %s has %s vectors for %s chunks — embedding live instead",
                  cache_path, vectors.shape[0], len(chunk_texts))
        return None

    log.info("loaded %s precomputed embeddings from %s — no embedding calls needed",
             vectors.shape[0], cache_path)
    return vectors
