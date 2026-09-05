"""
Local vector store for the RAG corpus, deployed copy. Bedrock-only here
(unlike the multi-backend dev version in the main ship-engine repo) — this
code runs inside AgentCore Runtime, which is already Bedrock's own
environment, so there's no reason for a local/Ollama fallback path here.
"""

import json

import numpy as np

from aws.bedrock_session import bedrock_session
from rag.chunker import Chunk

TITAN_EMBED_MODEL_ID = "amazon.titan-embed-text-v2:0"


def embed_texts(texts: list[str]) -> np.ndarray:
    # finding #45: built from the shared rate-limited session so a cold-
    # start VectorStore.build() burst (one call per corpus chunk) is paced
    # against the same account-wide quota as this container's own LLM
    # completion calls, instead of being unpaced entirely.
    client = bedrock_session().client("bedrock-runtime")
    vectors = []
    for text in texts:
        body = json.dumps({"inputText": text})
        response = client.invoke_model(modelId=TITAN_EMBED_MODEL_ID, body=body)
        payload = json.loads(response["body"].read())
        vectors.append(payload["embedding"])
    return np.array(vectors, dtype=np.float32)


class VectorStore:
    def __init__(self):
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None

    def build(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        self._vectors = embed_texts([c.text for c in chunks])

    def query(self, text: str, top_k: int = 3):
        if self._vectors is None or len(self._chunks) == 0:
            raise RuntimeError("VectorStore.build() must be called before query().")

        query_vec = embed_texts([text])[0]
        norms = np.linalg.norm(self._vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-8
        scores = (self._vectors @ query_vec) / norms

        top_indices = np.argsort(-scores)[:top_k]
        return [(self._chunks[i], float(scores[i])) for i in top_indices]
