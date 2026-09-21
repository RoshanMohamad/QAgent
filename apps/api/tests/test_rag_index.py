"""Retrieval: code-aware tokenisation, BM25 ranking, vector search, and the
invariant that retrieval works with no provider configured.
"""

from __future__ import annotations

from qagent.modules.rag.chunker import CodeChunk
from qagent.modules.rag.embeddings import NullEmbedder, build_embedder
from qagent.modules.rag.index import (
    HybridRetriever,
    LexicalIndex,
    VectorIndex,
    build_index,
    tokenize,
)


def _chunk(path: str, text: str, symbol: str | None = None, line: int = 1) -> CodeChunk:
    return CodeChunk(
        path=path, start_line=line, end_line=line + 5, text=text, symbol=symbol, language="python"
    )


# ---------------------------------------------------------------- tokenisation


def test_snake_case_is_split_and_kept_whole() -> None:
    assert set(tokenize("create_order")) >= {"create_order", "create", "order"}


def test_camel_case_is_split() -> None:
    """`createOrder` in a query has to reach `create_order` in the code."""
    assert set(tokenize("createOrder")) >= {"createorder", "create", "order"}


def test_stopwords_and_single_characters_are_dropped() -> None:
    tokens = tokenize("return the x for a value")

    assert "return" not in tokens
    assert "the" not in tokens
    assert "x" not in tokens
    assert "value" in tokens


def test_acronyms_survive_splitting() -> None:
    assert "http" in tokenize("HTTPError")


# -------------------------------------------------------------------- lexical


def test_ranks_the_implementing_function_first() -> None:
    index = LexicalIndex.build(
        [
            _chunk("orders.py", "def create_order(item):\n    return item.price", "create_order"),
            _chunk("health.py", "def healthz():\n    return 'ok'", "healthz"),
            _chunk("docs.py", "def render_docs():\n    return None", "render_docs"),
        ]
    )

    hits = index.search("create_order", k=3)

    assert hits[0].chunk.symbol == "create_order"
    assert hits[0].retriever == "lexical"


def test_symbol_match_outranks_a_mere_mention() -> None:
    """A query for a function should prefer its definition over its call sites."""
    index = LexicalIndex.build(
        [
            _chunk(
                "caller.py",
                "def handler():\n    create_order()\n    create_order()\n    create_order()",
                "handler",
            ),
            _chunk("orders.py", "def create_order(item):\n    return item", "create_order"),
        ]
    )

    assert index.search("create_order", k=2)[0].chunk.symbol == "create_order"


def test_no_match_returns_nothing_rather_than_the_least_bad_chunk() -> None:
    index = LexicalIndex.build([_chunk("a.py", "def alpha():\n    pass", "alpha")])

    assert index.search("kubernetes ingress annotations") == []


def test_empty_index_and_empty_query_are_safe() -> None:
    assert LexicalIndex.build([]).search("anything") == []
    assert LexicalIndex.build([_chunk("a.py", "x = 1")]).search("") == []


def test_k_limits_the_result_count() -> None:
    index = LexicalIndex.build([_chunk(f"m{i}.py", "def order():\n    pass", "order") for i in range(9)])

    assert len(index.search("order", k=3)) == 3


def test_ranking_is_deterministic() -> None:
    chunks = [_chunk(f"m{i}.py", "def order():\n    pass", "order") for i in range(5)]
    index = LexicalIndex.build(chunks)

    first = [h.chunk.path for h in index.search("order", k=5)]
    second = [h.chunk.path for h in index.search("order", k=5)]

    assert first == second


# --------------------------------------------------------------------- vector


class _StubEmbedder:
    """Maps text to a 2-d vector by keyword, so similarity is checkable by hand."""

    name = "stub"
    available = True
    dimensions = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            lowered = text.lower()
            out.append([1.0, 0.0] if "payment" in lowered or "charge" in lowered else [0.0, 1.0])
        return out


def test_vector_search_finds_a_paraphrase_lexical_cannot() -> None:
    chunks = [
        _chunk("charge.py", "def charge_card(amount):\n    pass", "charge_card"),
        _chunk("misc.py", "def unrelated():\n    pass", "unrelated"),
    ]

    lexical = LexicalIndex.build(chunks)
    assert lexical.search("payment") == []

    vector = VectorIndex.build(chunks, _StubEmbedder())
    hits = vector.search("payment", k=2)

    assert hits[0].chunk.symbol == "charge_card"
    assert hits[0].retriever == "vector"


def test_chunks_that_failed_to_embed_are_dropped_not_zeroed() -> None:
    class _PartialEmbedder:
        name = "partial"
        available = True
        dimensions = 2

        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] if i == 0 else [] for i in range(len(texts))]

    vector = VectorIndex.build([_chunk("a.py", "x"), _chunk("b.py", "y")], _PartialEmbedder())

    assert len(vector.chunks) == 1
    assert vector.chunks[0].path == "a.py"


# --------------------------------------------------------------------- hybrid


def test_hybrid_fuses_both_rankings() -> None:
    chunks = [
        _chunk("charge.py", "def charge_card(amount):\n    pass", "charge_card"),
        _chunk("orders.py", "def create_order(item):\n    pass", "create_order"),
    ]
    hybrid = HybridRetriever(
        retrievers=[LexicalIndex.build(chunks), VectorIndex.build(chunks, _StubEmbedder())]
    )

    # Lexical alone finds nothing for "payment"; the fused ranking still does.
    assert hybrid.search("payment", k=2)[0].chunk.symbol == "charge_card"
    # And an exact identifier still wins.
    assert hybrid.search("create_order", k=2)[0].chunk.symbol == "create_order"


def test_hybrid_with_one_retriever_delegates_to_it() -> None:
    chunks = [_chunk("orders.py", "def create_order():\n    pass", "create_order")]
    hybrid = HybridRetriever(retrievers=[LexicalIndex.build(chunks)])

    assert hybrid.search("create_order", k=1)[0].retriever == "lexical"


def test_hybrid_with_no_retrievers_returns_nothing() -> None:
    assert HybridRetriever(retrievers=[]).search("anything") == []


# --------------------------------------------------------------------- wiring


def test_default_configuration_gives_working_lexical_retrieval() -> None:
    """The invariant: no provider, no network, retrieval still works."""
    embedder = build_embedder()
    assert not embedder.available

    index = build_index(
        [_chunk("orders.py", "def create_order():\n    pass", "create_order")],
        embedder=embedder,
    )

    assert not index.embedded
    assert index.summary()["retriever"] == "lexical"
    assert index.search("create_order")[0].chunk.symbol == "create_order"


def test_null_embedder_returns_empty_vectors() -> None:
    assert NullEmbedder().embed(["a", "b"]) == [[], []]


def test_an_embedder_that_raises_degrades_to_lexical() -> None:
    class _BrokenEmbedder:
        name = "broken"
        available = True
        dimensions = 2

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("provider down")

    index = build_index([_chunk("a.py", "def alpha():\n    pass", "alpha")], embedder=_BrokenEmbedder())

    assert not index.embedded
    assert index.search("alpha")


def test_summary_counts_what_was_indexed() -> None:
    index = build_index([_chunk("a.py", "x = 1"), _chunk("b.py", "def f():\n    pass", "f")])

    assert index.summary() == {
        "chunks": 2,
        "files": 2,
        "symbols": 1,
        "embedded": False,
        "retriever": "lexical",
    }


# ---------------------------------------------------------------- stemming


def test_plural_route_reaches_singular_handler() -> None:
    """`/orders` must find `create_order`, or retrieval fails the ordinary case."""
    index = LexicalIndex.build(
        [
            _chunk("orders.py", "def create_order(item):\n    return item", "create_order"),
            _chunk("misc.py", "def unrelated():\n    pass", "unrelated"),
        ]
    )

    hits = index.search("orders", k=2)

    assert hits
    assert hits[0].chunk.symbol == "create_order"


def test_singularize_leaves_identifiers_alone() -> None:
    from qagent.modules.rag.index import singularize

    assert singularize("status") == "status"
    assert singularize("address") == "address"
    assert singularize("orders") == "order"
    assert singularize("categories") == "category"
    assert singularize("boxes") == "box"
    assert singularize("api") == "api"
