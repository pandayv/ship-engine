"""
Local vector store for the RAG corpus. Deliberately NOT FAISS/Chroma-as-a-
service or Bedrock Knowledge Bases — the corpus is ~13 chunks (GDPR Art. 32
+ EU AI Act Art. 14), so a brute-force numpy cosine-similarity search is
simpler, has zero install risk, and is functionally identical at this scale.
See ship_roadmap.md's "AWS-native technical stack" section for why Knowledge
Bases was deliberately skipped.

Embedding backend is switchable via the SHIP_MODEL_BACKEND env var
("bedrock" | "ollama", default "bedrock" — the account-wide Bedrock quota
block that once made "ollama" the safe default was resolved 2026-09-04, see
ship_roadmap.md; this docstring previously went stale on that point,
finding #55 in the 2026-09-05 architecture review — fixed here). Bedrock
Titan is the AWS-native choice for the real submission; Ollama's
nomic-embed-text remains available as a fully-local, zero-AWS-dependency
fallback.

Backend selection matters for more than dev convenience: Bedrock is the
primary submission backend, Gemini is the deliberate credit-exhaustion
fallback (free tier, independent of AWS), and Ollama is the fully-local
reliability fallback (works with no internet/API dependency at all). All
three need to work completely — including embeddings, not just the LLM
call — or "switch to Gemini" as a real fallback plan doesn't actually work
when it's needed. gemini embeddings use Google's `gemini-embedding-001`
model via the same google-genai client/GEMINI_API_KEY already required for
Diagnostician's Gemini LLM path.
"""

import json
import os
from dataclasses import dataclass

import numpy as np

from src.rag.chunker import Chunk

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
OLLAMA_EMBED_MODEL_ID = "nomic-embed-text"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
GEMINI_EMBED_MODEL_ID = "gemini-embedding-001"  # text-only; confirmed current via
# live web search 2026-09-05, not guessed — the sibling "gemini-embedding-2" model
# is multimodal (text/image/video/audio) and unnecessary for this text-only corpus.


def _embed_bedrock(texts: list[str]) -> np.ndarray:
    from src.aws.bedrock_session import bedrock_session  # lazy import, same reasoning as before

    # finding #45: built from the shared rate-limited session so a cold-
    # start VectorStore.build() burst (one call per corpus chunk) is paced
    # against the same account-wide quota as Diagnostician's own LLM calls,
    # instead of being unpaced entirely.
    client = bedrock_session().client("bedrock-runtime")
    vectors = []
    for text in texts:
        body = json.dumps({"inputText": text})
        response = client.invoke_model(modelId=TITAN_EMBED_MODEL_ID, body=body)
        payload = json.loads(response["body"].read())
        vectors.append(payload["embedding"])
    return np.array(vectors, dtype=np.float32)


def _embed_ollama(texts: list[str]) -> np.ndarray:
    import ollama  # lazy import

    client = ollama.Client(host=OLLAMA_HOST)
    vectors = [client.embeddings(model=OLLAMA_EMBED_MODEL_ID, prompt=text)["embedding"] for text in texts]
    return np.array(vectors, dtype=np.float32)


def _embed_gemini(texts: list[str], is_query: bool) -> np.ndarray:
    # Verified against the actually-installed google-genai SDK (2026-09-05,
    # via live introspection, not guessed): Client().models.embed_content(
    # model=..., contents=<list[str] ok>) -> EmbedContentResponse, whose
    # .embeddings is a list[ContentEmbedding], each with a .values vector —
    # same shape/order as the input list, matching _embed_bedrock's contract.
    #
    # task_type matters, confirmed by a real failure caught during live
    # testing, not assumed: without it, a live query for "sending raw
    # personal data to an external AI model" ranked OWASP chunks above the
    # actually-relevant GDPR Art. 32 chunk — silently wrong retrieval, no
    # error anywhere, exactly the failure mode "Trust but Verify" exists to
    # catch. Gemini's embedding model is asymmetric: short queries and long
    # documents need different task_type hints (RETRIEVAL_QUERY vs.
    # RETRIEVAL_DOCUMENT) to land in a comparable embedding space. Verified
    # valid values via a live web search (2026-09-05), not guessed — the
    # SDK's own type hint for this field is an unconstrained string.
    from google import genai
    from google.genai import types

    client = genai.Client()  # reads GEMINI_API_KEY from env automatically
    task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
    response = client.models.embed_content(
        model=GEMINI_EMBED_MODEL_ID, contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return np.array([e.values for e in response.embeddings], dtype=np.float32)


def embed_texts(texts: list[str], is_query: bool = False) -> np.ndarray:
    """
    is_query: whether these texts are a live search query (vs. corpus
    documents being indexed) — only meaningful for the gemini backend,
    where it materially affects retrieval quality (see _embed_gemini).
    Ignored by bedrock/ollama, which don't have this asymmetric-embedding
    concept in the same way.
    """
    backend = os.environ.get("SHIP_MODEL_BACKEND", "bedrock")
    if backend == "bedrock":
        return _embed_bedrock(texts)
    elif backend == "ollama":
        return _embed_ollama(texts)
    elif backend == "gemini":
        return _embed_gemini(texts, is_query=is_query)
    raise ValueError(f"Unknown SHIP_MODEL_BACKEND: {backend!r} (expected 'bedrock', 'ollama', or 'gemini')")


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float


class VectorStore:
    def __init__(self):
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None

    def build(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        self._vectors = embed_texts([c.text for c in chunks], is_query=False)

    def query(self, text: str, top_k: int = 3) -> list[RetrievedChunk]:
        if self._vectors is None or len(self._chunks) == 0:
            raise RuntimeError("VectorStore.build() must be called before query().")

        query_vec = embed_texts([text], is_query=True)[0]
        # cosine similarity, brute force — fine at this corpus size
        norms = np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-8
        scores = (self._vectors @ query_vec) / norms

        top_indices = np.argsort(-scores)[:top_k]
        return [RetrievedChunk(chunk=self._chunks[i], score=float(scores[i])) for i in top_indices]
