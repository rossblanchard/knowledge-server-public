"""Ollama embedding client.

Embeddings are L2-normalized at creation so cosine similarity reduces
to a dot product at query time. The mxbai instruction prefix is applied
to queries only, per the model card — documents embed raw.
"""

import httpx
import numpy as np

from . import config


class Embedder:
    def __init__(
        self,
        base_url: str = config.OLLAMA_URL,
        model: str = config.EMBED_MODEL,
    ):
        self.model = model
        self.client = httpx.Client(base_url=base_url, timeout=config.EMBED_TIMEOUT_S)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        """Embed document chunks. Returns (n, EMBED_DIM) float32, normalized."""
        vecs: list[np.ndarray] = []
        bs = config.EMBED_BATCH_SIZE
        for i in range(0, len(texts), bs):
            vecs.extend(self._embed(texts[i : i + bs]))
        return np.vstack(vecs) if vecs else np.empty((0, config.EMBED_DIM), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        """Embed a search query with the mxbai retrieval prefix."""
        return self._embed([config.QUERY_PREFIX + text])[0]

    def _embed(self, inputs: list[str]) -> list[np.ndarray]:
        resp = self.client.post(
            "/api/embed",
            json={
                "model": self.model,
                "input": inputs,
                "truncate": True,
                "keep_alive": config.KEEP_ALIVE,
            },
        )
        resp.raise_for_status()
        embeddings = resp.json()["embeddings"]
        out: list[np.ndarray] = []
        for emb in embeddings:
            v = np.asarray(emb, dtype=np.float32)
            if v.shape != (config.EMBED_DIM,):
                raise ValueError(
                    f"unexpected embedding shape {v.shape}, expected ({config.EMBED_DIM},)"
                )
            norm = float(np.linalg.norm(v))
            if norm > 0.0:
                v = v / norm
            out.append(v)
        return out

    def close(self):
        self.client.close()
