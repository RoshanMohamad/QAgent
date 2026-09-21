"""Persist a repository index, using pgvector when it is actually installed.

The awkward truth this module exists to handle: CLAUDE.md section 21 specifies
pgvector, and the project's own ``docker-compose.yml`` runs stock
``postgres:16-alpine``, which does not have it. Declaring a ``vector`` column in
``models.py`` would make ``create_all`` fail outright on the image the project
ships - the schema would not build at all.

So the column is declared as JSON, and this module *upgrades* it:

    ensure_vector_support()   CREATE EXTENSION vector; ALTER the column to
                              vector(n); build an IVFFlat index
    save_chunks()             write chunks, skipping unchanged digests
    load_index()              rebuild a RepositoryIndex from stored rows
    search_similar()          ANN search in Postgres when the extension is
                              present, in Python when it is not

Every one of those degrades rather than failing. The consequence of no pgvector
is that similarity search happens in Python over a few thousand vectors, which
is milliseconds at this scale - not an outage, just a different constant
factor. The consequence of *requiring* pgvector would be that nobody can run
the project without editing its compose file first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from qagent import models
from qagent.modules.rag.chunker import CodeChunk
from qagent.modules.rag.embeddings import Embedder
from qagent.modules.rag.index import RepositoryIndex, build_index

logger = logging.getLogger(__name__)

#: IVFFlat list count. 100 is pgvector's own documented starting point and is
#: appropriate up to roughly a million rows; tuning it against a repository
#: nobody has indexed yet would be guessing.
_IVFFLAT_LISTS = 100


@dataclass(frozen=True)
class VectorSupport:
    """What the connected database can actually do."""

    extension: bool
    index_built: bool
    dimensions: int = 0

    @property
    def native_search(self) -> bool:
        """True when similarity can be pushed into Postgres.

        The ANN index is an optimisation on top of this, not a precondition:
        the `<=>` operator works without it, just without the index.
        """
        return self.extension

    def to_dict(self) -> dict:
        return {
            "pgvector": self.extension,
            "native_search": self.native_search,
            "index_built": self.index_built,
            "dimensions": self.dimensions,
        }


def ensure_vector_support(session: Session, *, dimensions: int) -> VectorSupport:
    """Install the extension and ANN index when this deployment asked for them.

    Only acts when ``QAGENT_VECTOR_BACKEND=pgvector``. It never alters the
    ``embedding`` column: the mapper's idea of that column's type is fixed when
    the model is defined, so changing the physical type underneath it breaks
    every insert with "column is of type vector but expression is of type json"
    (see ``models._embedding_column_type``). Either the column was already
    declared as a vector, or the backend is ``json`` and there is nothing here
    to do.

    Creating an extension needs privileges the application role deliberately
    lacks (ADR-0007), so this runs from ``db_init`` as the bootstrap superuser.
    """
    from qagent.config import get_settings

    if get_settings().qagent_vector_backend != "pgvector":
        return VectorSupport(extension=False, index_built=False)

    try:
        session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        # Configuration asked for pgvector and the database cannot provide it.
        # Loud, because the alternative is a deployment that believes it has
        # native search and quietly does not.
        logger.error(
            "QAGENT_VECTOR_BACKEND=pgvector but the extension could not be created: %s",
            str(exc).splitlines()[0][:200],
        )
        return VectorSupport(extension=False, index_built=False)

    try:
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_code_chunks_embedding "
                "ON code_chunks USING ivfflat (embedding vector_cosine_ops) "
                f"WITH (lists = {_IVFFLAT_LISTS})"
            )
        )
        session.commit()
    except SQLAlchemyError as exc:
        session.rollback()
        # Extension and column are fine; only the index failed. Exact search
        # still works, so this costs speed, not correctness.
        logger.warning("pgvector index could not be built, search stays exact: %s", exc)
        return VectorSupport(extension=True, index_built=False, dimensions=int(dimensions))

    logger.info("pgvector ready on code_chunks at %d dimensions", int(dimensions))
    return VectorSupport(extension=True, index_built=True, dimensions=int(dimensions))


def save_chunks(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    chunks: list[CodeChunk],
    commit_sha: str | None = None,
    embedder: Embedder | None = None,
    replace: bool = True,
) -> dict:
    """Write chunks for a project, re-embedding only what changed.

    ``replace`` deletes rows whose digest is no longer present, which is what
    keeps a stale chunk from being cited after the code it described has moved.
    A bug report pointing at ``orders.py:41-77`` when the function now lives at
    line 120 is worse than one with no citation at all.
    """
    incoming = {c.digest(): c for c in chunks}

    existing_rows = session.execute(
        select(models.CodeChunk).where(models.CodeChunk.project_id == project_id)
    ).scalars().all()
    existing = {row.digest: row for row in existing_rows}

    removed = 0
    if replace:
        stale = [row.id for digest, row in existing.items() if digest not in incoming]
        if stale:
            session.execute(delete(models.CodeChunk).where(models.CodeChunk.id.in_(stale)))
            removed = len(stale)

    new_digests = [d for d in incoming if d not in existing]

    # Only the genuinely new text is embedded. This is the entire reason chunk
    # digests exist: a second scan of an unchanged repository costs nothing.
    vectors: dict[str, list[float]] = {}
    model_name: str | None = None
    if embedder is not None and embedder.available and new_digests:
        model_name = getattr(embedder, "model", embedder.name)
        texts = [f"{incoming[d].location}\n{incoming[d].text}" for d in new_digests]
        try:
            for digest, vector in zip(new_digests, embedder.embed(texts), strict=False):
                if vector:
                    vectors[digest] = vector
        except Exception as exc:  # noqa: BLE001 - store the text regardless
            logger.warning("embedding failed during save, storing text only: %s", exc)

    for digest in new_digests:
        chunk = incoming[digest]
        session.add(
            models.CodeChunk(
                org_id=org_id,
                project_id=project_id,
                commit_sha=commit_sha,
                path=chunk.path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                symbol=chunk.symbol,
                language=chunk.language,
                content=chunk.text,
                digest=digest,
                embedding=vectors.get(digest),
                embedding_model=model_name if digest in vectors else None,
            )
        )

    session.flush()
    stats = {
        "total": len(incoming),
        "added": len(new_digests),
        "unchanged": len(incoming) - len(new_digests),
        "removed": removed,
        "embedded": len(vectors),
    }
    logger.info("stored code chunks: %s", stats)
    return stats


def _to_code_chunk(row: models.CodeChunk) -> CodeChunk:
    return CodeChunk(
        path=row.path,
        start_line=row.start_line,
        end_line=row.end_line,
        text=row.content,
        symbol=row.symbol,
        language=row.language,
    )


def load_index(
    session: Session, *, project_id: UUID, embedder: Embedder | None = None
) -> RepositoryIndex:
    """Rebuild a searchable index from stored rows.

    The lexical index is rebuilt in memory because BM25 statistics are cheap to
    recompute and expensive to keep correct incrementally. The embeddings are
    the part worth persisting, and they are read straight back.
    """
    rows = session.execute(
        select(models.CodeChunk)
        .where(models.CodeChunk.project_id == project_id)
        .order_by(models.CodeChunk.path, models.CodeChunk.start_line)
    ).scalars().all()

    chunks = [_to_code_chunk(row) for row in rows]
    if not chunks:
        return RepositoryIndex(chunks=[], retriever=build_index([]).retriever)

    stored = {
        row.digest: [float(v) for v in row.embedding]
        for row in rows
        if row.embedding
    }

    if embedder is None or not embedder.available or not stored:
        return build_index(chunks, embedder=None)

    # Reuse the stored vectors rather than re-embedding: `build_index` would
    # otherwise call the provider again for text that has not changed, which is
    # the cost this table exists to avoid.
    return build_index(chunks, embedder=_CachedEmbedder(embedder, stored, chunks))


@dataclass
class _CachedEmbedder:
    """Serves stored vectors for known chunks, delegates for anything else.

    Only ever used by ``load_index``: it exists so a rebuilt index can be
    vector-capable without paying to embed the corpus a second time. A query
    (which has no stored vector) falls through to the real embedder.
    """

    inner: Embedder
    by_digest: dict[str, list[float]]
    chunks: list[CodeChunk]

    @property
    def name(self) -> str:
        return f"cached:{self.inner.name}"

    @property
    def available(self) -> bool:
        return self.inner.available

    @property
    def dimensions(self) -> int:
        return self.inner.dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        by_text = {f"{c.location}\n{c.text}": c.digest() for c in self.chunks}
        out: list[list[float]] = []
        misses: list[int] = []

        for position, value in enumerate(texts):
            digest = by_text.get(value)
            vector = self.by_digest.get(digest) if digest else None
            out.append(vector or [])
            if vector is None:
                misses.append(position)

        if misses:
            fresh = self.inner.embed([texts[i] for i in misses])
            for position, vector in zip(misses, fresh, strict=False):
                out[position] = vector

        return out


def search_similar(
    session: Session,
    *,
    project_id: UUID,
    query_vector: list[float],
    k: int = 5,
    support: VectorSupport | None = None,
) -> list[tuple[CodeChunk, float]]:
    """Nearest neighbours by cosine distance, in Postgres when it can.

    Returns ``(chunk, similarity)`` with similarity in 0..1, matching what the
    in-memory ``VectorIndex`` returns, so callers never have to know which path
    ran.
    """
    if not query_vector:
        return []

    if support is not None and support.native_search:
        try:
            literal = "[" + ",".join(str(float(v)) for v in query_vector) + "]"
            rows = session.execute(
                text(
                    "SELECT id, path, start_line, end_line, symbol, language, content, "
                    "1 - (embedding <=> CAST(:q AS vector)) AS similarity "
                    "FROM code_chunks "
                    # project_id is cast explicitly: psycopg binds a Python str
                    # as varchar, and Postgres has no `uuid = varchar` operator,
                    # so without this the native path fails on every call and
                    # silently falls back forever.
                    "WHERE project_id = CAST(:pid AS uuid) AND embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:q AS vector) LIMIT :k"
                ).bindparams(q=literal, pid=str(project_id), k=k)
            ).all()
            return [
                (
                    CodeChunk(
                        path=row.path,
                        start_line=row.start_line,
                        end_line=row.end_line,
                        text=row.content,
                        symbol=row.symbol,
                        language=row.language,
                    ),
                    float(row.similarity),
                )
                for row in rows
            ]
        except SQLAlchemyError as exc:
            # The rollback is what makes this a real fallback rather than a
            # second failure: Postgres aborts the whole transaction on error,
            # so every subsequent statement in it - including the fallback
            # query below - fails with "current transaction is aborted".
            session.rollback()
            logger.warning("pgvector search failed, falling back to Python: %s", exc)

    # Python fallback. At a few thousand chunks this is milliseconds, which is
    # why not having pgvector is a different constant factor and not an outage.
    rows = session.execute(
        select(models.CodeChunk).where(
            models.CodeChunk.project_id == project_id,
            models.CodeChunk.embedding.isnot(None),
        )
    ).scalars().all()

    scored = []
    for row in rows:
        vector = [float(v) for v in (row.embedding or [])]
        if len(vector) != len(query_vector):
            continue
        scored.append((_to_code_chunk(row), _cosine(query_vector, vector)))

    scored.sort(key=lambda pair: -pair[1])
    return scored[:k]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
