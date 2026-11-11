"""Text embeddings for vendor knowledge, computed locally (no API, no cost).

The default is fastembed's ONNX build of `BAAI/bge-small-en-v1.5` (384
dimensions, matching `vendor_knowledge.embedding vector(384)`), downloaded once
from Hugging Face and run on the CPU. `KHATA_EMBEDDER=hash` swaps in a
deterministic hashing embedder, which tests use so they need no download.
Embeddings only rank what the Analyst reads first; no money decision uses them.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from typing import Protocol

log = logging.getLogger(__name__)

DIM = 384
DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """bge-small via fastembed. The model loads on first use, off the event loop."""

    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        self.name = f"fastembed:{model}"
        self._model_name = model
        self._model = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self._model_name)
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        def run() -> list[list[float]]:
            return [v.tolist() for v in self._load().embed(texts)]

        return await asyncio.to_thread(run)


class HashEmbedder:
    """Bag of words hashed into 384 buckets, L2-normalised. Deterministic; for tests."""

    name = "hash"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:4], "big")
                v[h % DIM] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def default_embedder() -> Embedder:
    kind = os.environ.get("KHATA_EMBEDDER", "fastembed")
    if kind == "hash":
        return HashEmbedder()
    return FastEmbedder(os.environ.get("KHATA_EMBED_MODEL", DEFAULT_MODEL))


def to_pgvector(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
