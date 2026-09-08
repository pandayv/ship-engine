#!/usr/bin/env python3
"""
Regenerates the precomputed corpus embeddings. Run this whenever the
regulation corpus changes — adding an article, editing text, or switching
the embedding model.

    python3 scripts/precompute_embeddings.py

Needs AWS credentials (it makes one Titan embedding call per chunk, once).
That cost is the entire point: paying it here, at build time, means no
container pays it again at cold start. See src/rag/embedding_cache.py for
the failure this exists to fix.

Writes BOTH corpus copies — the main repo's and the deployed AgentCore
container's. Writing only one is the exact silent-drift failure that
finding #29 already cost this project a day of dead detectors, so this
script deliberately does not offer a way to update just one of them.

Forgetting to run this is not dangerous, only slow: a stale cache fails
its fingerprint check at load time and the system embeds live instead.
tests/test_embedding_cache.py turns that slow path into a visible test
failure so it gets noticed here rather than in production.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.rag.chunker import chunk_corpus  # noqa: E402
from src.rag.embedding_cache import default_cache_path, save  # noqa: E402
from src.rag.vector_store import active_embed_model_id, embed_texts  # noqa: E402

CORPUS_DIRS = (
    REPO_ROOT / "rag_corpus",
    REPO_ROOT / "shipagentcore" / "app" / "ship_diagnostician" / "rag_corpus",
)


def main() -> int:
    model_id = active_embed_model_id()
    print(f"Embedding model: {model_id}")

    # Embed the primary corpus once, then reuse those vectors for the
    # mirrored copy — but only after proving the two corpora are actually
    # identical. If they have drifted, reusing the vectors would bake that
    # drift into the artifact and hide it behind a passing fingerprint.
    primary, mirror = CORPUS_DIRS
    primary_chunks = chunk_corpus(primary)
    mirror_chunks = chunk_corpus(mirror)

    primary_texts = [c.text for c in primary_chunks]
    mirror_texts = [c.text for c in mirror_chunks]

    if primary_texts != mirror_texts:
        print(
            f"REFUSING TO WRITE: the two corpus copies differ.\n"
            f"  {primary}: {len(primary_texts)} chunks\n"
            f"  {mirror}: {len(mirror_texts)} chunks\n"
            f"Reconcile them first — a cache built from a diverged corpus would "
            f"pass its own fingerprint check while serving the wrong text.",
            file=sys.stderr,
        )
        return 1

    if not primary_texts:
        print(f"REFUSING TO WRITE: no chunks found under {primary}", file=sys.stderr)
        return 1

    print(f"Chunks: {len(primary_texts)} — embedding now (this is the call burst "
          f"that no longer happens at runtime)")
    vectors = embed_texts(primary_texts, is_query=False)
    print(f"Embedded: {vectors.shape[0]} vectors of {vectors.shape[1]} dimensions")

    for corpus_dir in CORPUS_DIRS:
        path = default_cache_path(corpus_dir)
        save(primary_texts, model_id, vectors, path)
        print(f"Wrote {path.relative_to(REPO_ROOT)} ({path.stat().st_size / 1024:.0f} KB)")

    print("\nDone. Commit both files, and redeploy the AgentCore runtime so the "
          "container picks up its copy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
