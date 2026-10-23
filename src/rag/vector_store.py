"""
Local vector store for the RAG corpus. A brute-force numpy cosine-similarity search, not
FAISS/Chroma-as-a-service or Bedrock Knowledge Bases: the corpus is small (GDPR Art. 32, EU AI
Act Art. 14 and the other sourced texts), so brute force is simpler, has zero install risk, and
is functionally identical at this scale.

The embedding backend is switchable via SHIP_MODEL_BACKEND ("bedrock" | "ollama" | "gemini",
default "bedrock"). Bedrock Titan is the AWS-native choice. Gemini (free tier, independent of
AWS) and Ollama (fully local, no internet or API dependency) are fallbacks, and all three work
completely, embeddings included, not only the LLM call. Gemini embeddings use Google's
`gemini-embedding-001` model via the same google-genai client and GEMINI_API_KEY as Detector's
Gemini LLM path.
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from src.rag import embedding_cache
from src.rag.chunker import Chunk

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
OLLAMA_EMBED_MODEL_ID = "nomic-embed-text"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
GEMINI_EMBED_MODEL_ID = "gemini-embedding-001"  # text-only; the sibling "gemini-embedding-2"
# model is multimodal and unnecessary for this text-only corpus.


@lru_cache(maxsize=1)
def _bedrock_runtime_client():
    # One bedrock-runtime client per process, built from the cached session, instead of a new
    # client on every RAG query (including every retrieve_regulation_text tool call during a
    # fragment's detection).
    from src.aws.bedrock_session import BEDROCK_RETRY_CONFIG, bedrock_session  # lazy import, same reasoning as before

    return bedrock_session().client("bedrock-runtime", config=BEDROCK_RETRY_CONFIG)


def _embed_bedrock(texts: list[str]) -> np.ndarray:
    # Built with adaptive retry config (BEDROCK_RETRY_CONFIG), so a cold-start
    # VectorStore.build() burst (one call per corpus chunk), or concurrent traffic from other
    # fragments, backs off and retries automatically if the account quota is hit, instead of
    # being unpaced or delayed by a local limiter that can't see other processes' traffic.
    client = _bedrock_runtime_client()
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
    # Client().models.embed_content(model=..., contents=<list[str]>) returns an
    # EmbedContentResponse whose .embeddings is a list[ContentEmbedding], each with a .values
    # vector, in the same order as the input, matching _embed_bedrock's contract.
    #
    # task_type matters. Without it, a query like "sending raw personal data to an external AI
    # model" ranked OWASP chunks above the relevant GDPR Art. 32 chunk, which is silently wrong
    # retrieval. Gemini's embedding model is asymmetric: short queries and long documents need
    # different task_type hints (RETRIEVAL_QUERY vs. RETRIEVAL_DOCUMENT) to land in a comparable
    # embedding space. The SDK's own type hint for this field is an unconstrained string.
    from google import genai
    from google.genai import types

    client = genai.Client()  # reads GEMINI_API_KEY from env automatically
    task_type = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
    response = client.models.embed_content(
        model=GEMINI_EMBED_MODEL_ID, contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return np.array([e.values for e in response.embeddings], dtype=np.float32)


def active_embed_model_id() -> str:
    """
    Identifies the embedding space currently in use, backend included.

    Backend is part of the identity, not just the model name: Titan,
    Gemini and Ollama embeddings are mutually incompatible vector spaces,
    and mixing precomputed vectors from one with live queries from another
    produces no error — only silently wrong retrieval. This string goes
    into the embedding cache's fingerprint so that mistake is impossible
    rather than merely unlikely (see src/rag/embedding_cache.py).
    """
    backend = os.environ.get("SHIP_MODEL_BACKEND", "bedrock")
    model = {
        "bedrock": TITAN_EMBED_MODEL_ID,
        "ollama": OLLAMA_EMBED_MODEL_ID,
        "gemini": GEMINI_EMBED_MODEL_ID,
    }.get(backend)
    if model is None:
        raise ValueError(f"Unknown SHIP_MODEL_BACKEND: {backend!r} (expected 'bedrock', 'ollama', or 'gemini')")
    return f"{backend}:{model}"


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

    def build(self, chunks: list[Chunk], cache_path: Path | None = None) -> None:
        """
        cache_path: a precomputed-embeddings artifact to use if — and only
        if — it provably matches these exact chunks and the active
        embedding model. A missing, stale or mismatched cache is not an
        error; it just means embedding live, which is slower but always
        correct. See src/rag/embedding_cache.py for why that direction of
        failure is the safe one.
        """
        self._chunks = chunks
        texts = [c.text for c in chunks]

        if cache_path is not None:
            cached = embedding_cache.load(texts, active_embed_model_id(), cache_path)
            if cached is not None:
                self._vectors = cached
                return

        self._vectors = embed_texts(texts, is_query=False)

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
