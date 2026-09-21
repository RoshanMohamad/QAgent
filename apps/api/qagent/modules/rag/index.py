"""Retrieval over a chunked checkout.

Two retrievers, one interface, and the cheap one is the default:

* **Lexical (BM25).** Pure Python, no dependencies, no provider, no network. On
  code this is not a toy fallback - identifier overlap is a genuinely strong
  signal, because the thing you are searching for (`create_order`, `product_id`,
  the path `/orders`) usually appears *verbatim* in the code that implements it.
* **Vector.** Cosine similarity over embeddings, when an embedding provider is
  configured. Better at the paraphrase cases lexical search misses entirely
  ("payment fails" -> `ChargeService`).

`HybridRetriever` runs both and fuses the rankings. Every one of them satisfies
the same invariant the rest of QAgent holds to: with no provider configured and
no network, retrieval still works and still returns useful results.

Tokenisation is code-aware. `create_order` has to match a query for
`createOrder`, or retrieval fails on exactly the identifiers that matter most,
so identifiers are split on case boundaries and underscores and indexed both
whole and in parts.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from qagent.modules.rag.chunker import CodeChunk

if TYPE_CHECKING:
    from qagent.modules.rag.embeddings import Embedder

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

#: Terms that appear in nearly every source file. BM25's IDF already discounts
#: them, but dropping them outright keeps short queries from being dominated by
#: the one stopword they happen to contain.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "if", "is", "in", "to", "of", "for", "on",
        "at", "by", "as", "it", "be", "this", "that", "with", "from", "not",
        "return", "import", "export", "const", "let", "var", "def", "self",
        "none", "null", "true", "false", "function", "class", "new",
    }
)

# BM25 parameters. These are the standard defaults; they are not tuned here
# because tuning them against one repository would be overfitting to it.
_K1 = 1.5
_B = 0.75


def singularize(token: str) -> str:
    """Crudest useful stemming: plural nouns to singular.

    This exists for one specific, extremely common failure. The route is
    ``/orders``; the handler is ``create_order``. Without this, a query built
    from the path shares no term at all with the code that implements it, and
    retrieval returns nothing for the most ordinary case there is.

    A real stemmer (Porter, Snowball) would be a dependency and would mangle
    identifiers - ``status`` becomes ``statu``, ``address`` becomes ``address``
    only by luck. These three rules cover English plurals in route names, which
    is the entire job.
    """
    if len(token) <= 3 or token.endswith("ss") or token.endswith("us"):
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("ches", "shes", "xes", "zes", "ses")):
        return token[:-2]
    if token.endswith("s"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    """Split text into search terms, keeping identifiers both whole and split.

    ``create_order`` yields ``create_order``, ``create``, ``order``. That
    redundancy is deliberate: an exact identifier match should outrank a match
    on one of its halves, and emitting both is what makes that fall out of BM25
    naturally instead of needing a special case.

    Singular forms are emitted alongside plurals for the same reason, so a query
    built from ``/orders`` reaches ``create_order``.
    """
    tokens: list[str] = []

    def add(token: str) -> None:
        if len(token) > 1 and token not in _STOPWORDS:
            tokens.append(token)
            stem = singularize(token)
            if stem != token:
                tokens.append(stem)

    for raw in _WORD_RE.findall(text):
        add(raw.lower())
        parts = [p.lower() for p in _CAMEL_RE.findall(raw)]
        if len(parts) > 1:
            for part in parts:
                add(part)
    return tokens


@dataclass
class Hit:
    chunk: CodeChunk
    score: float
    retriever: str

    def to_dict(self) -> dict:
        return {
            "location": self.chunk.location,
            "path": self.chunk.path,
            "symbol": self.chunk.symbol,
            "start_line": self.chunk.start_line,
            "end_line": self.chunk.end_line,
            "score": round(self.score, 4),
            "retriever": self.retriever,
        }


class Retriever(Protocol):
    name: str

    def search(self, query: str, *, k: int = 5) -> list[Hit]: ...


@dataclass
class LexicalIndex:
    """BM25 over chunk text, plus a bonus for matching the symbol name.

    The symbol bonus is the one deviation from textbook BM25 and it earns its
    place: a query for ``create_order`` should prefer the function *called*
    ``create_order`` over the twelve call sites that merely mention it.
    """

    chunks: list[CodeChunk] = field(default_factory=list)
    name: str = "lexical"

    _term_freqs: list[Counter] = field(default_factory=list, repr=False)
    _doc_freq: Counter = field(default_factory=Counter, repr=False)
    _lengths: list[int] = field(default_factory=list, repr=False)
    _avg_length: float = 0.0

    #: Multiplier applied when a query term appears in the chunk's symbol name.
    SYMBOL_BOOST = 1.6

    @classmethod
    def build(cls, chunks: list[CodeChunk]) -> LexicalIndex:
        index = cls(chunks=list(chunks))
        for chunk in index.chunks:
            terms = Counter(tokenize(chunk.text))
            index._term_freqs.append(terms)
            index._lengths.append(sum(terms.values()))
            index._doc_freq.update(terms.keys())
        index._avg_length = (
            sum(index._lengths) / len(index._lengths) if index._lengths else 0.0
        )
        logger.info(
            "lexical index: %d chunks, %d distinct terms", len(index.chunks), len(index._doc_freq)
        )
        return index

    def _idf(self, term: str) -> float:
        n = len(self.chunks)
        df = self._doc_freq.get(term, 0)
        # BM25's probabilistic IDF, floored at zero so a term present in every
        # chunk contributes nothing rather than pushing the score negative.
        return max(0.0, math.log(1 + (n - df + 0.5) / (df + 0.5)))

    def search(self, query: str, *, k: int = 5) -> list[Hit]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []

        scored: list[Hit] = []
        for position, chunk in enumerate(self.chunks):
            freqs = self._term_freqs[position]
            length = self._lengths[position] or 1
            symbol_terms = set(tokenize(chunk.symbol or ""))

            score = 0.0
            for term in terms:
                tf = freqs.get(term, 0)
                if not tf:
                    continue
                numerator = tf * (_K1 + 1)
                denominator = tf + _K1 * (1 - _B + _B * length / (self._avg_length or 1))
                contribution = self._idf(term) * numerator / denominator
                if term in symbol_terms:
                    contribution *= self.SYMBOL_BOOST
                score += contribution

            if score > 0:
                scored.append(Hit(chunk=chunk, score=score, retriever=self.name))

        scored.sort(key=lambda h: (-h.score, h.chunk.path, h.chunk.start_line))
        return scored[:k]


@dataclass
class VectorIndex:
    """Cosine similarity over embedded chunks.

    Built only when an embedder is configured. The vectors are held in memory
    here; ``store.py`` is what persists them to pgvector so an index survives
    the worker process that built it.
    """

    chunks: list[CodeChunk] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    embedder: Embedder | None = None
    name: str = "vector"

    @classmethod
    def build(cls, chunks: list[CodeChunk], embedder: Embedder) -> VectorIndex:
        texts = [f"{c.location}\n{c.text}" for c in chunks]
        vectors = embedder.embed(texts)
        kept = [(c, v) for c, v in zip(chunks, vectors, strict=False) if v]
        logger.info("vector index: embedded %d/%d chunks", len(kept), len(chunks))
        return cls(
            chunks=[c for c, _ in kept],
            vectors=[_normalize(v) for _, v in kept],
            embedder=embedder,
        )

    def search(self, query: str, *, k: int = 5) -> list[Hit]:
        if not self.chunks or self.embedder is None:
            return []
        embedded = self.embedder.embed([query])
        if not embedded or not embedded[0]:
            return []
        query_vector = _normalize(embedded[0])

        scored = [
            Hit(chunk=chunk, score=_dot(query_vector, vector), retriever=self.name)
            for chunk, vector in zip(self.chunks, self.vectors, strict=False)
        ]
        scored.sort(key=lambda h: (-h.score, h.chunk.path, h.chunk.start_line))
        return [h for h in scored[:k] if h.score > 0]


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


@dataclass
class HybridRetriever:
    """Reciprocal rank fusion over the retrievers that are available.

    RRF rather than a weighted score sum, because the two retrievers produce
    scores on incomparable scales - a BM25 score of 8.0 and a cosine similarity
    of 0.81 cannot be added without inventing a conversion. Fusing on *rank*
    needs no such invention, which is why it is the standard answer.
    """

    retrievers: list[Retriever] = field(default_factory=list)
    name: str = "hybrid"

    #: RRF's damping constant. 60 is the value from the original paper and the
    #: one every implementation uses; it keeps rank 1 from dominating entirely.
    K_RRF = 60

    def search(self, query: str, *, k: int = 5) -> list[Hit]:
        active = [r for r in self.retrievers if r is not None]
        if not active:
            return []
        if len(active) == 1:
            return active[0].search(query, k=k)

        fused: dict[tuple[str, int], tuple[CodeChunk, float, list[str]]] = {}
        for retriever in active:
            for rank, hit in enumerate(retriever.search(query, k=k * 3)):
                key = (hit.chunk.path, hit.chunk.start_line)
                contribution = 1.0 / (self.K_RRF + rank + 1)
                if key in fused:
                    chunk, score, sources = fused[key]
                    fused[key] = (chunk, score + contribution, [*sources, retriever.name])
                else:
                    fused[key] = (hit.chunk, contribution, [retriever.name])

        ranked = sorted(fused.values(), key=lambda entry: -entry[1])
        return [
            Hit(chunk=chunk, score=score, retriever="+".join(sources))
            for chunk, score, sources in ranked[:k]
        ]


@dataclass
class RepositoryIndex:
    """What a caller actually holds: the chunks plus whichever retrievers exist."""

    chunks: list[CodeChunk]
    retriever: Retriever
    embedded: bool = False

    @property
    def size(self) -> int:
        return len(self.chunks)

    def search(self, query: str, *, k: int = 5) -> list[Hit]:
        return self.retriever.search(query, k=k)

    def summary(self) -> dict:
        files = {c.path for c in self.chunks}
        return {
            "chunks": len(self.chunks),
            "files": len(files),
            "symbols": sum(1 for c in self.chunks if c.symbol),
            "embedded": self.embedded,
            "retriever": self.retriever.name,
        }


def build_index(chunks: list[CodeChunk], *, embedder: Embedder | None = None) -> RepositoryIndex:
    """Build the best index the configuration allows.

    With no embedder this is BM25 alone, which is a real retriever and not a
    degraded mode - the README's claim that every feature works with no provider
    configured has to keep holding here too.
    """
    lexical = LexicalIndex.build(chunks)

    if embedder is None or not embedder.available or not chunks:
        return RepositoryIndex(chunks=chunks, retriever=lexical, embedded=False)

    try:
        vector = VectorIndex.build(chunks, embedder)
    except Exception as exc:  # noqa: BLE001 - embedding is an upgrade, never a requirement
        logger.warning("vector index build failed, continuing lexical-only: %s", exc)
        return RepositoryIndex(chunks=chunks, retriever=lexical, embedded=False)

    if not vector.chunks:
        return RepositoryIndex(chunks=chunks, retriever=lexical, embedded=False)

    return RepositoryIndex(
        chunks=chunks,
        retriever=HybridRetriever(retrievers=[lexical, vector]),
        embedded=True,
    )
