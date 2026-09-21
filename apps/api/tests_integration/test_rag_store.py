"""Repository RAG against a real Postgres: chunks persist, re-indexing an
unchanged checkout costs nothing, stale chunks are removed, and similarity search
works identically on both vector backends.

That last point is the one worth a real database to prove. The project's own
compose file runs stock `postgres:16-alpine`, which has no vector extension, so
the JSON backend is not a hypothetical fallback - it is the path almost everyone
will actually run. CI runs this file twice, once per backend.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import sessionmaker

from qagent import models
from qagent.db import set_tenant
from qagent.modules.rag.chunker import CodeChunk
from qagent.modules.rag.store import (
    ensure_vector_support,
    load_index,
    save_chunks,
    search_similar,
)


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


def _seed_project(session):
    org = models.Organization(id=uuid.uuid4(), name="rag-org", slug=f"rag-{uuid.uuid4().hex[:8]}")
    session.add(org)
    session.commit()

    set_tenant(session, org.id)
    project = models.Project(org_id=org.id, name="indexed")
    session.add(project)
    session.flush()
    return org, project


def _commit(session, org_id) -> None:
    """Commit, then re-bind the tenant.

    `set_tenant` is SET LOCAL, so it dies with the transaction; a query issued
    after a commit without re-binding fails closed on an invalid ''::uuid cast
    (db.py's docstring, ADR-0007). Wrapping it here rather than repeating the
    pair at nine call sites keeps the tests about RAG.
    """
    session.commit()
    set_tenant(session, org_id)


def _chunk(path: str, text: str, symbol: str | None = None, line: int = 1) -> CodeChunk:
    return CodeChunk(
        path=path, start_line=line, end_line=line + 4, text=text, symbol=symbol, language="python"
    )


class _StubEmbedder:
    """Two-dimensional, keyword-driven, and counts its calls."""

    name = "stub"
    model = "stub-embed-v1"
    available = True
    dimensions = 2

    def __init__(self) -> None:
        self.embedded = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded += len(texts)
        return [
            [1.0, 0.0] if ("payment" in t.lower() or "charge" in t.lower()) else [0.0, 1.0]
            for t in texts
        ]


# ------------------------------------------------------------------ persistence


def test_chunks_round_trip_through_the_database(Session) -> None:
    with Session() as session:
        org, project = _seed_project(session)

        stats = save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[
                _chunk("orders.py", "def create_order(item):\n    return item.price", "create_order"),
                _chunk("health.py", "def healthz():\n    return 'ok'", "healthz"),
            ],
            commit_sha="abc123",
        )
        _commit(session, org.id)

        assert stats["added"] == 2
        assert stats["total"] == 2

        index = load_index(session, project_id=project.id)
        assert index.size == 2
        assert index.search("create_order")[0].chunk.symbol == "create_order"


def test_reindexing_unchanged_source_adds_nothing(Session) -> None:
    """The whole point of storing digests: a second scan must be free."""
    chunks = [_chunk("orders.py", "def create_order():\n    pass", "create_order")]

    with Session() as session:
        org, project = _seed_project(session)

        save_chunks(session, org_id=org.id, project_id=project.id, chunks=chunks)
        _commit(session, org.id)

        second = save_chunks(session, org_id=org.id, project_id=project.id, chunks=chunks)
        _commit(session, org.id)

        assert second["added"] == 0
        assert second["unchanged"] == 1
        assert second["removed"] == 0


def test_moved_code_does_not_leave_a_stale_citation_behind(Session) -> None:
    """A chunk citing a line the function has moved off is worse than no citation."""
    with Session() as session:
        org, project = _seed_project(session)

        save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[_chunk("orders.py", "def create_order():\n    pass", "create_order", line=41)],
        )
        _commit(session, org.id)

        stats = save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[_chunk("orders.py", "def create_order():\n    pass", "create_order", line=120)],
        )
        _commit(session, org.id)

        assert stats["removed"] == 1
        index = load_index(session, project_id=project.id)
        assert index.size == 1
        assert index.chunks[0].start_line == 120


def test_an_empty_project_loads_an_empty_index(Session) -> None:
    with Session() as session:
        _, project = _seed_project(session)

        index = load_index(session, project_id=project.id)

        assert index.size == 0
        assert index.search("anything") == []


# -------------------------------------------------------------------- embeddings


def test_stored_vectors_are_reused_instead_of_re_embedded(Session) -> None:
    embedder = _StubEmbedder()
    chunks = [_chunk("charge.py", "def charge_card(amount):\n    pass", "charge_card")]

    with Session() as session:
        org, project = _seed_project(session)

        save_chunks(
            session, org_id=org.id, project_id=project.id, chunks=chunks, embedder=embedder
        )
        _commit(session, org.id)
        after_save = embedder.embedded
        assert after_save == 1

        index = load_index(session, project_id=project.id, embedder=embedder)
        assert index.embedded

        # Rebuilding the index must not pay to embed the corpus again; only the
        # query itself reaches the provider.
        assert embedder.embedded == after_save

        hits = index.search("payment", k=1)
        assert hits and hits[0].chunk.symbol == "charge_card"


def test_embedding_model_is_recorded_with_the_vector(Session) -> None:
    with Session() as session:
        org, project = _seed_project(session)

        save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[_chunk("a.py", "def alpha():\n    pass", "alpha")],
            embedder=_StubEmbedder(),
        )
        _commit(session, org.id)

        row = session.query(models.CodeChunk).one()
        assert row.embedding_model == "stub-embed-v1"
        assert row.embedding


def test_text_is_stored_even_when_embedding_fails(Session) -> None:
    class _BrokenEmbedder:
        name = "broken"
        available = True
        dimensions = 2

        def embed(self, texts):
            raise RuntimeError("provider down")

    with Session() as session:
        org, project = _seed_project(session)

        stats = save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[_chunk("a.py", "def alpha():\n    pass", "alpha")],
            embedder=_BrokenEmbedder(),
        )
        _commit(session, org.id)

        assert stats["added"] == 1
        assert stats["embedded"] == 0
        # Lexical retrieval still works, which is the invariant that matters.
        assert load_index(session, project_id=project.id).search("alpha")


# ------------------------------------------------------------------- similarity


def test_similarity_search_works_with_or_without_pgvector(Session, admin_engine) -> None:
    from sqlalchemy.orm import Session as RawSession

    with RawSession(admin_engine) as admin:
        support = ensure_vector_support(admin, dimensions=2)

    with Session() as session:
        org, project = _seed_project(session)

        save_chunks(
            session,
            org_id=org.id,
            project_id=project.id,
            chunks=[
                _chunk("charge.py", "def charge_card(amount):\n    pass", "charge_card"),
                _chunk("misc.py", "def unrelated():\n    pass", "unrelated"),
            ],
            embedder=_StubEmbedder(),
        )
        _commit(session, org.id)

        results = search_similar(
            session, project_id=project.id, query_vector=[1.0, 0.0], k=2, support=support
        )

        assert results
        assert results[0][0].symbol == "charge_card"
        assert 0.0 <= results[0][1] <= 1.0
        # Whichever path ran, the answer is the same. That equivalence is the
        # claim: pgvector is an optimisation here, never a requirement. This
        # suite is run against both backends in CI for exactly that reason.


def test_support_matches_the_configured_backend(admin_engine) -> None:
    """A dishonest capability report is how a silent fallback becomes an
    unexplained performance cliff months later."""
    import os

    from sqlalchemy.orm import Session as RawSession

    with RawSession(admin_engine) as admin:
        support = ensure_vector_support(admin, dimensions=2)

    if os.environ.get("QAGENT_VECTOR_BACKEND") == "pgvector":
        assert support.extension and support.native_search
    else:
        # The default backend must never claim native search, whether or not
        # the extension happens to exist in this database.
        assert not support.extension
        assert not support.native_search


def test_no_query_vector_returns_nothing(Session) -> None:
    with Session() as session:
        _, project = _seed_project(session)

        assert search_similar(session, project_id=project.id, query_vector=[]) == []


# ----------------------------------------------------------------- tenant scope


def test_chunks_are_scoped_to_their_project(Session) -> None:
    with Session() as session:
        org, project_a = _seed_project(session)
        project_b = models.Project(org_id=org.id, name="other")
        session.add(project_b)
        session.flush()

        save_chunks(
            session,
            org_id=org.id,
            project_id=project_a.id,
            chunks=[_chunk("a.py", "def alpha():\n    pass", "alpha")],
        )
        save_chunks(
            session,
            org_id=org.id,
            project_id=project_b.id,
            chunks=[_chunk("b.py", "def beta():\n    pass", "beta")],
        )
        _commit(session, org.id)

        assert load_index(session, project_id=project_a.id).chunks[0].symbol == "alpha"
        assert load_index(session, project_id=project_b.id).chunks[0].symbol == "beta"
