"""
Local vector store for the RAG corpus. Deliberately NOT FAISS/Chroma-as-a-
service or Bedrock Knowledge Bases — the corpus is ~13 chunks (GDPR Art. 32
+ EU AI Act Art. 14), so a brute-force numpy cosine-similarity search is
simpler, has zero install risk, and is functionally identical at this scale.
See ship_roadmap.md's "AWS-native technical stack" section for why Knowledge
Bases was deliberately skipped.

Embeddings come from Amazon Bedrock Titan (the decided AWS-native choice) —
this module WILL raise a clear error if AWS credentials aren't configured,
which is expected until the user completes the AWS Builder ID / account
setup. Nothing here is mocked or faked.
"""

import json
from dataclasses import dataclass

import numpy as np

from src.rag.chunker import Chunk

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"


def embed_texts(texts: list[str]) -> np.ndarray:
    """
    Calls Bedrock Titan Embeddings for each text. Requires AWS credentials
    configured (aws configure / AWS Builder ID + account setup) — will raise
    botocore.exceptions.NoCredentialsError or similar if not.
    """
    import boto3  # imported lazily so the rest of this module is importable
                  # without boto3 configured, for testing chunker/store logic

    client = boto3.client("bedrock-runtime")
    vectors = []
    for text in texts:
        body = json.dumps({"inputText": text})
        response = client.invoke_model(modelId=TITAN_EMBED_MODEL_ID, body=body)
        payload = json.loads(response["body"].read())
        vectors.append(payload["embedding"])
    return np.array(vectors, dtype=np.float32)


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
