#!/usr/bin/env python3
"""
Pattern Miner — Detector's self-improvement loop. Extracts durable,
generalized patterns from human-dismissed findings, so Detector gets
better at telling real violations from look-alikes over time, without
growing an ever-larger few-shot example bag and without a human having to
approve every extracted pattern. See src/rag/pattern_store.py's module
docstring for the full reasoning behind both of those design choices.

Same shape as scripts/healing_loop.py on purpose: scheduled/on-demand,
never on Detector's per-fragment hot path. This does one real Bedrock call
per NEW dismissal since the last run, not a full re-scan of history —
already-mined dismissals are never reprocessed.

Algorithm, per new dismissal (resolution == "rejected", i.e. a human
determined this specific finding was NOT actually a violation):
  1. Ask the model for one general, reusable sentence explaining why this
     kind of case isn't a violation — generalized from this specific
     instance, not restating its specific values.
  2. Embed that candidate sentence and compare it against every existing
     pattern (same cosine-similarity retrieval as Detector's own query
     path). A close match (>= SIMILARITY_THRESHOLD) is reinforcement of
     an existing pattern (support_count += 1) — not a new, near-duplicate
     row. Anything else becomes a new pattern with support_count = 1.
  3. A pattern only affects a future judgment once support_count reaches
     MIN_SUPPORT (src/rag/pattern_store.py) — reinforced by independent
     dismissals, not created by one person's single call.

Cursor: the resolved_at of the last dismissal processed, stored as a
sentinel row in ship-learned-patterns itself (pattern_store.CURSOR_ID) —
one row, not a second table, at this scale.
"""

from __future__ import annotations

import json

SIMILARITY_THRESHOLD = 0.85
MODEL_ID = "amazon.nova-lite-v1:0"  # same production model Detector uses — see detector.py's benchmark

EXTRACTION_PROMPT = """A human reviewer determined the following flagged code \
change was NOT actually a compliance violation.

Taxonomy category: {taxonomy_id}
What Detector originally flagged: {plain_english_summary}
The human's reason for dismissing it: {resolution_reason}

State ONE general, reusable sentence describing the underlying PATTERN that made \
this a false positive — generalized so it applies to similar future cases, not a \
restatement of this specific instance's file names or values. Respond with only \
that one sentence, nothing else."""


def _new_rejected_alerts_since(cursor: str | None):
    from src.storage.alert_store import list_resolved_alerts

    alerts = [a for a in list_resolved_alerts() if a.resolution == "rejected"]
    if cursor:
        alerts = [a for a in alerts if a.resolved_at and a.resolved_at > cursor]
    return sorted(alerts, key=lambda a: a.resolved_at or "")


def _extract_pattern_text(alert) -> str:
    from src.aws.bedrock_session import BEDROCK_RETRY_CONFIG, bedrock_session

    client = bedrock_session().client("bedrock-runtime", config=BEDROCK_RETRY_CONFIG)

    prompt = EXTRACTION_PROMPT.format(
        taxonomy_id=alert.taxonomy_id,
        plain_english_summary=alert.plain_english_summary,
        resolution_reason=alert.resolution_reason or "(no reason given)",
    )
    response = client.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    return response["output"]["message"]["content"][0]["text"].strip()


def _find_reinforceable_pattern(candidate_text: str, existing) -> object | None:
    from src.rag.pattern_store import PatternStore

    if not existing:
        return None
    store = PatternStore()
    store.build(existing)
    ranked = store.query(candidate_text, top_k=1)
    if not ranked:
        return None
    # PatternStore.query() doesn't return a score, so recompute cosine
    # similarity directly for the one candidate — top_k=1 already found
    # the best match, this just checks whether it clears the bar.
    from src.rag.vector_store import embed_texts

    import numpy as np
    a = embed_texts([candidate_text], is_query=True)[0]
    b = embed_texts([ranked[0].text], is_query=False)[0]
    similarity = float((a @ b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))
    return ranked[0] if similarity >= SIMILARITY_THRESHOLD else None


def mine() -> dict:
    from src.rag import pattern_store

    cursor = pattern_store.get_cursor()
    new_alerts = _new_rejected_alerts_since(cursor)
    existing = pattern_store.list_patterns(eligible_only=False)

    reinforced, created = 0, 0
    for alert in new_alerts:
        candidate_text = _extract_pattern_text(alert)
        match = _find_reinforceable_pattern(candidate_text, existing)
        now = pattern_store.now_iso()

        if match is not None:
            match.support_count += 1
            match.last_reinforced = now
            if alert.taxonomy_id not in match.source_taxonomy_ids:
                match.source_taxonomy_ids = (*match.source_taxonomy_ids, alert.taxonomy_id)
            pattern_store.put_pattern(match)
            reinforced += 1
        else:
            new_pattern = pattern_store.LearnedPattern(
                pattern_id=pattern_store.new_pattern_id(),
                text=candidate_text,
                support_count=1,
                first_seen=now,
                last_reinforced=now,
                source_taxonomy_ids=(alert.taxonomy_id,),
            )
            pattern_store.put_pattern(new_pattern)
            existing.append(new_pattern)
            created += 1

    if new_alerts:
        pattern_store.set_cursor(new_alerts[-1].resolved_at)

    return {"processed": len(new_alerts), "reinforced": reinforced, "created": created}


def main() -> int:
    result = mine()
    print(json.dumps(result, indent=2))
    print(f"\n{result['processed']} new dismissal(s) processed: "
          f"{result['reinforced']} reinforced an existing pattern, "
          f"{result['created']} started a new one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
