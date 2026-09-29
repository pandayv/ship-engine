"""
Learned patterns extracted from human dismissals — deployed copy.

Mirrors src/rag/pattern_store.py in the main ship-engine repo; see that
file's docstring for the full design reasoning (why patterns, not
few-shot examples; why no per-pattern human approval gate). This copy is
Bedrock-only, same reasoning as rag/vector_store.py's own docstring in
this package — no backend switch needed inside AgentCore Runtime, which
is already Bedrock's own environment.

Reads and writes the SAME real ship-learned-patterns DynamoDB table the
main repo's scripts/pattern_miner.py writes to — this container never
runs the miner itself (that's a standalone batch job against
ship-alerts, nothing here), it only retrieves what the miner already
wrote, the same way it already retrieves the static regulation corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

TABLE_NAME = "ship-learned-patterns"
MIN_SUPPORT = 3
CURSOR_ID = "__cursor__"


@dataclass
class LearnedPattern:
    pattern_id: str
    text: str
    support_count: int
    first_seen: str
    last_reinforced: str
    source_taxonomy_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        return self.support_count >= MIN_SUPPORT


@lru_cache(maxsize=1)
def _table():
    import boto3

    return boto3.resource("dynamodb").Table(TABLE_NAME)


def _row_to_pattern(item: dict) -> LearnedPattern:
    return LearnedPattern(
        pattern_id=item["pattern_id"],
        text=item["text"],
        support_count=int(item["support_count"]),
        first_seen=item["first_seen"],
        last_reinforced=item["last_reinforced"],
        source_taxonomy_ids=tuple(item.get("source_taxonomy_ids", [])),
    )


def list_patterns(eligible_only: bool = True) -> list[LearnedPattern]:
    items = _table().scan().get("Items", [])
    patterns = [_row_to_pattern(i) for i in items if i["pattern_id"] != CURSOR_ID]
    return [p for p in patterns if p.eligible] if eligible_only else patterns


class PatternStore:
    """Same brute-force cosine-similarity retrieval as VectorStore in this
    package's rag/vector_store.py — see that file and the main repo's
    pattern_store.py for why patterns aren't modeled as a Chunk."""

    def __init__(self) -> None:
        self._patterns: list[LearnedPattern] = []
        self._vectors: np.ndarray | None = None

    def build(self, patterns: list[LearnedPattern]) -> None:
        from rag.vector_store import embed_texts

        self._patterns = patterns
        self._vectors = embed_texts([p.text for p in patterns]) if patterns else None

    def query(self, text: str, top_k: int = 3) -> list[LearnedPattern]:
        if not self._patterns or self._vectors is None:
            return []

        from rag.vector_store import embed_texts

        query_vec = embed_texts([text])[0]
        norms = np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-8
        scores = (self._vectors @ query_vec) / norms

        top_indices = np.argsort(-scores)[:top_k]
        return [self._patterns[i] for i in top_indices]
