"""Embedding providers for repository RAG.

Deliberately only two, mirroring ``modules/llm/providers.py``:

``null``
    Not available. Reports ``available = False`` so ``build_index`` stays
    lexical-only. This is the default, and it is why a fresh checkout with no
    API key still gets working retrieval.
``openai_compatible``
    Any OpenAI-shaped ``/v1/embeddings`` endpoint, which covers OpenAI itself,
    a local Ollama server, and most self-hosted gateways.

There is no Anthropic embedder because Anthropic does not serve an embeddings
API. Stubbing one that returned hashed pseudo-vectors would be worse than
having none: hashed vectors have no semantic geometry, so cosine similarity over
them is noise wearing the costume of a result, and it would quietly corrupt the
hybrid ranking. Lexical retrieval is the honest floor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from qagent.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Requests are batched because a per-chunk round trip over a 3,000-chunk repo
#: is minutes of latency spent entirely on HTTP overhead.
BATCH_SIZE = 64


class Embedder(Protocol):
    name: str
    available: bool
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class NullEmbedder:
    """The offline default. Never produces vectors and says so."""

    name: str = "null"
    available: bool = False
    dimensions: int = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


@dataclass
class OpenAiCompatibleEmbedder:
    """POST /v1/embeddings against any OpenAI-shaped endpoint.

    Called over httpx rather than through an SDK, the same choice the LLM
    providers made, so the package carries no vendor dependency.
    """

    base_url: str
    api_key: str | None
    model: str
    dimensions: int = 1536
    timeout_seconds: float = 60.0
    name: str = "openai_compatible"
    available: bool = True
    calls: int = field(default=0, init=False)

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed in batches, returning an empty vector for anything that failed.

        A partial failure degrades that chunk to lexical-only rather than
        failing the run: `VectorIndex.build` drops empty vectors, so a provider
        having a bad minute costs recall, not the indexing job.
        """
        if not texts:
            return []

        out: list[list[float]] = []
        url = self.base_url.rstrip("/") + "/embeddings"

        with httpx.Client(timeout=self.timeout_seconds) as client:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start : start + BATCH_SIZE]
                began = time.monotonic()
                try:
                    response = client.post(
                        url,
                        headers=self._headers(),
                        json={"model": self.model, "input": batch},
                    )
                    response.raise_for_status()
                    payload = response.json()
                except Exception as exc:  # noqa: BLE001 - degrade to lexical, never crash
                    logger.warning("embedding batch failed (%d texts): %s", len(batch), exc)
                    out.extend([[] for _ in batch])
                    continue

                self.calls += 1
                logger.debug(
                    "embedded %d texts in %dms", len(batch), int((time.monotonic() - began) * 1000)
                )

                data = payload.get("data") or []
                by_index = {item.get("index", i): item for i, item in enumerate(data)}
                for offset in range(len(batch)):
                    item = by_index.get(offset) or {}
                    vector = item.get("embedding") or []
                    out.append([float(v) for v in vector] if vector else [])

        return out


def build_embedder(settings: Settings | None = None) -> Embedder:
    """The configured embedder, or the null one.

    Falls back to null - loudly - rather than raising when the provider is set
    to ``openai_compatible`` without a base URL, because an indexing run that
    returns lexical results is far more useful than one that refuses to start.
    """
    settings = settings or get_settings()

    if settings.qagent_embedding_provider == "null":
        return NullEmbedder()

    base_url = settings.qagent_embedding_base_url or settings.qagent_llm_base_url
    if not base_url:
        logger.warning(
            "qagent_embedding_provider=openai_compatible but no base URL is set; "
            "falling back to lexical-only retrieval"
        )
        return NullEmbedder()

    return OpenAiCompatibleEmbedder(
        base_url=base_url,
        api_key=settings.qagent_embedding_api_key,
        model=settings.qagent_embedding_model,
        dimensions=settings.qagent_embedding_dimensions,
    )
