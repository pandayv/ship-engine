"""
Learned patterns extracted from human dismissals — Detector's
self-improvement loop.

WHY PATTERNS, NOT EXAMPLES. The first design considered here was few-shot:
feed Detector the raw text of recent dismissed findings directly. That
doesn't survive real usage — after a month or two of dismissals there are
too many raw examples to fit in a prompt, and picking a recent subset is
arbitrary. Instead, a periodic batch job (scripts/pattern_miner.py, same
shape as scripts/healing_loop.py: scheduled, off Detector's hot path)
extracts a small, bounded set of GENERALIZED patterns from accumulated
dismissals, merging new evidence into existing patterns rather than
appending — the set stays small regardless of how much history exists.

WHY NO HUMAN APPROVAL GATE ON EACH PATTERN. Considered and rejected
directly: a pattern is often an abstraction tied to field-level data
associations ("hashed identifiers aren't PII"), not a business rule a
human can confidently judge in isolation, and asking for sign-off on every
one would be real, recurring toil for no real safety gain. Two guarards
replace it instead, both automatic:
  - MIN_SUPPORT — a pattern only becomes retrieval-eligible once
    reinforced by independent dismissals (not one person's one-off call).
  - Retrieved as CONTEXT, never a rule. Detector's grounded,
    regulation-citation judgment still runs on every fragment regardless
    of what a pattern says — see retrieve_prior_determinations() in
    src/agents/detector.py. One bad pattern cannot cause a silent false
    negative on its own, because the underlying judgment never stops
    running just because a pattern was retrieved alongside it.

Same brute-force cosine-similarity retrieval as src/rag/vector_store.py,
not the same class — a pattern isn't a regulation Chunk (source_file/
article/paragraph_index don't mean anything for it), and this table is
small (bounded by MIN_SUPPORT and periodic merging) so a fresh per-
invocation rebuild from DynamoDB is fine at this scale, same reasoning
vector_store.py's own docstring gives for the ~13-chunk regulation corpus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

import numpy as np

log = logging.getLogger(__name__)

TABLE_NAME = "ship-learned-patterns"

# A pattern seen in only one dismissal is one person's one-off call, not a
# durable rule — see module docstring. 3 independent reinforcements is the
# bar before a pattern is allowed to influence a future judgment.
MIN_SUPPORT = 3

# Reserved pattern_id for the mining cursor (scripts/pattern_miner.py) —
# a sentinel row rather than a second table, to avoid a whole extra table
# for one timestamp at this scale.
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


def create_table_if_not_exists() -> None:
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("dynamodb")
    try:
        client.describe_table(TableName=TABLE_NAME)
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    client.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pattern_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pattern_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    client.get_waiter("table_exists").wait(TableName=TABLE_NAME)


def _row_to_pattern(item: dict) -> LearnedPattern:
    return LearnedPattern(
        pattern_id=item["pattern_id"],
        text=item["text"],
        support_count=int(item["support_count"]),
        first_seen=item["first_seen"],
        last_reinforced=item["last_reinforced"],
        source_taxonomy_ids=tuple(item.get("source_taxonomy_ids", [])),
    )


def put_pattern(pattern: LearnedPattern) -> None:
    _table().put_item(Item={
        "pattern_id": pattern.pattern_id,
        "text": pattern.text,
        "support_count": pattern.support_count,
        "first_seen": pattern.first_seen,
        "last_reinforced": pattern.last_reinforced,
        "source_taxonomy_ids": list(pattern.source_taxonomy_ids),
    })


def list_patterns(eligible_only: bool = True) -> list[LearnedPattern]:
    """Every learned pattern (excluding the mining cursor sentinel). Small
    scale by design — see module docstring — so an unpaginated scan is
    fine; MIN_SUPPORT and merging keep this table bounded, unlike
    ship-alerts, which is why this doesn't need _scan_all()'s pagination
    loop."""
    items = _table().scan().get("Items", [])
    patterns = [_row_to_pattern(i) for i in items if i["pattern_id"] != CURSOR_ID]
    return [p for p in patterns if p.eligible] if eligible_only else patterns


def get_cursor() -> str | None:
    item = _table().get_item(Key={"pattern_id": CURSOR_ID}).get("Item")
    return item["last_mined_at"] if item else None


def set_cursor(last_mined_at: str) -> None:
    _table().put_item(Item={"pattern_id": CURSOR_ID, "last_mined_at": last_mined_at})


def new_pattern_id() -> str:
    import uuid

    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PatternStore:
    """Brute-force cosine-similarity retrieval over currently-eligible
    patterns, built fresh from DynamoDB each time — same math as
    src/rag/vector_store.py's VectorStore, kept separate because a pattern
    isn't a regulation Chunk. See module docstring for why no caching is
    needed at this scale."""

    def __init__(self) -> None:
        self._patterns: list[LearnedPattern] = []
        self._vectors: np.ndarray | None = None

    def build(self, patterns: list[LearnedPattern]) -> None:
        from src.rag.vector_store import embed_texts

        self._patterns = patterns
        self._vectors = embed_texts([p.text for p in patterns], is_query=False) if patterns else None

    def query(self, text: str, top_k: int = 3) -> list[LearnedPattern]:
        if not self._patterns or self._vectors is None:
            return []

        from src.rag.vector_store import embed_texts

        query_vec = embed_texts([text], is_query=True)[0]
        norms = np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-8
        scores = (self._vectors @ query_vec) / norms

        top_indices = np.argsort(-scores)[:top_k]
        return [self._patterns[i] for i in top_indices]
