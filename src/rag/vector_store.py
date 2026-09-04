"""
Local vector store for the RAG corpus. Deliberately NOT FAISS/Chroma-as-a-
service or Bedrock Knowledge Bases — the corpus is ~13 chunks (GDPR Art. 32
+ EU AI Act Art. 14), so a brute-force numpy cosine-similarity search is
simpler, has zero install risk, and is functionally identical at this scale.
See ship_roadmap.md's "AWS-native technical stack" section for why Knowledge
Bases was deliberately skipped.

Embedding backend is switchable via the SHIP_MODEL_BACKEND env var
("bedrock" | "ollama", default "ollama" as of 2026-09-04). Bedrock Titan is
the intended AWS-native choice for the real submission, but the account's
Bedrock access is currently blocked by an unrelated new-account activation
issue (see ship_roadmap.md) — Ollama's nomic-embed-text runs fully local,
zero AWS dependency, so POC/dev work isn't blocked on that resolving.
Swapping back to Bedrock is a one-line env var change, not a code change.
"""

import json
import os
from dataclasses import dataclass

import numpy as np

from src.rag.chunker import Chunk

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"
OLLAMA_EMBED_MODEL_ID = "nomic-embed-text"
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _embed_bedrock(texts: list[str]) -> np.ndarray:
    import boto3  # lazy import, same reasoning as before

    client = boto3.client("bedrock-runtime")
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


def embed_texts(texts: list[str]) -> np.ndarray:
    backend = os.environ.get("SHIP_MODEL_BACKEND", "bedrock")
    if backend == "bedrock":
        return _embed_bedrock(texts)
    elif backend == "ollama":
        return _embed_ollama(texts)
    raise ValueError(f"Unknown SHIP_MODEL_BACKEND: {backend!r} (expected 'bedrock' or 'ollama')")


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
        self._vectors = embed_texts([c.text for c in chunks])

    def query(self, text: str, top_k: int = 3) -> list[RetrievedChunk]:
        if self._vectors is None or len(self._chunks) == 0:
            raise RuntimeError("VectorStore.build() must be called before query().")

        query_vec = embed_texts([text])[0]
        # cosine similarity, brute force — fine at this corpus size
        norms = np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-8
        scores = (self._vectors @ query_vec) / norms

        top_indices = np.argsort(-scores)[:top_k]
        return [RetrievedChunk(chunk=self._chunks[i], score=float(scores[i])) for i in top_indices]
