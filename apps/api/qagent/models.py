"""Relational model.

Design notes that differ from the original plan in CLAUDE.md section 20:

* org_id is present on every tenant-owned table from day one and backed by Postgres
  RLS. Retrofitting multi-tenancy in a later phase means rewriting every query ever
  written.
* artifacts exists as a table. Object storage was named in the plan but nothing
  pointed into it.
* llm_calls records tokens and cost per call, linked to agent_runs. This is what
  makes budgets, the eval harness and any future billing possible.
* test_results keeps a per-case history so flakiness can be derived from a pass/fail
  *sequence* rather than a boolean.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- enums


class EnvironmentMode(enum.StrEnum):
    """See ADR-0001. These are the only two supported input contracts."""

    TARGET_URL = "target_url"
    COMPOSE = "compose"


class RunStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"


class ResultStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FailureClass(enum.StrEnum):
    """CLAUDE.md section 14. A failing test is not automatically a defect."""

    REAL_BUG = "real_bug"
    FLAKY_TEST = "flaky_test"
    ENVIRONMENT = "environment"
    NETWORK = "network"
    DEPENDENCY = "dependency"
    TEST_DATA = "test_data"
    BAD_ASSERTION = "bad_assertion"
    UNKNOWN = "unknown"


class TestKind(enum.StrEnum):
    API_FUNCTIONAL = "api_functional"
    API_SECURITY = "api_security"
    E2E = "e2e"
    E2E_INTERACTIVE = "e2e_interactive"


# --------------------------------------------------------------------------- tenancy


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    projects: Mapped[list[Project]] = relationship(back_populates="organization")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_users_org_email"),)


# --------------------------------------------------------------------------- project


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500))
    default_branch: Mapped[str] = mapped_column(String(100), default="main", nullable=False)

    # Output of the Project Analyst agent (CLAUDE.md section 8, agent 1).
    stack: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: Where to send alerts, and for which events:
    #: ``{"channel": "slack", "target": "https://...", "events": [...]}``.
    #:
    #: Per project rather than per deployment because this platform is
    #: multi-tenant: one org's defects must not page another org's channel.
    #: Empty means notifications are off, which is the default - a QA tool that
    #: starts posting to a webhook nobody configured is a tool people mute.
    #:
    #: The target is a URL this server will fetch, so it goes through the same
    #: SSRF guard as every other user-supplied address (modules/notify).
    notify: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="projects")
    environments: Mapped[list[Environment]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Environment(Base, TimestampMixin):
    """A place tests can run against. See ADR-0001."""

    __tablename__ = "environments"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), default="default", nullable=False)
    mode: Mapped[EnvironmentMode] = mapped_column(
        Enum(EnvironmentMode, name="environment_mode"),
        default=EnvironmentMode.TARGET_URL,
        nullable=False,
    )
    base_url: Mapped[str | None] = mapped_column(String(500))
    openapi_url: Mapped[str | None] = mapped_column(String(500))
    e2e_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interactive_exploration_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    # Non-secret request defaults. Credentials live behind secret_ref, never here.
    default_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(200))

    project: Mapped[Project] = relationship(back_populates="environments")


class ApiEndpoint(Base, TimestampMixin):
    """One discovered operation. Populated by modules/discovery."""

    __tablename__ = "api_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)

    parameters: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    request_schema: Mapped[dict | None] = mapped_column(JSON)
    responses: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    requires_auth: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="openapi", nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    __table_args__ = (
        UniqueConstraint("project_id", "method", "path", name="uq_endpoint_project_method_path"),
        Index("ix_endpoints_project_risk", "project_id", "risk_score"),
    )


# --------------------------------------------------------------------------- tests


class TestSuite(Base, TimestampMixin):
    __tablename__ = "test_suites"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[TestKind] = mapped_column(
        Enum(TestKind, name="test_kind"), default=TestKind.API_FUNCTIONAL, nullable=False
    )

    cases: Mapped[list[TestCase]] = relationship(
        back_populates="suite", cascade="all, delete-orphan"
    )


class TestCase(Base, TimestampMixin):
    """A single executable check.

    spec is the declarative request/assertion document interpreted by modules/runner.
    Keeping it declarative rather than generated code is what makes generated tests
    reviewable, diffable and safe to execute.
    """

    __tablename__ = "test_cases"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suite_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_suites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_endpoints.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[TestKind] = mapped_column(
        Enum(TestKind, name="test_kind"), default=TestKind.API_FUNCTIONAL, nullable=False
    )
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)

    generated_by: Mapped[str] = mapped_column(String(32), default="rule", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flake_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    suite: Mapped[TestSuite] = relationship(back_populates="cases")


class TestRun(Base, TimestampMixin):
    __tablename__ = "test_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL")
    )

    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.PENDING, nullable=False
    )
    trigger: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    commit_sha: Mapped[str | None] = mapped_column(String(64))

    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: The strategy this run was held to, and how much of it actually ran
    #: (agent 2, modules/planner/strategy.py). Stored on the run rather than the
    #: project because the plan is derived from whatever discovery found *at that
    #: moment*: a run that covered 60% of its plan stays a true statement about
    #: that run even after the next deploy changes the API surface.
    plan: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    results: Mapped[list[TestResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class TestResult(Base, TimestampMixin):
    __tablename__ = "test_results"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    test_case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_cases.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[ResultStatus] = mapped_column(Enum(ResultStatus, name="result_status"))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    request: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    response: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    assertions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    failure_message: Mapped[str | None] = mapped_column(Text)

    failure_class: Mapped[FailureClass | None] = mapped_column(
        Enum(FailureClass, name="failure_class")
    )
    triage_confidence: Mapped[float | None] = mapped_column(Float)

    run: Mapped[TestRun] = relationship(back_populates="results")

    # Flakiness is a property of a sequence of results, not of one row.
    __table_args__ = (Index("ix_results_case_history", "test_case_id", "created_at"),)


class Artifact(Base, TimestampMixin):
    """Pointer into object storage. Missing entirely from the original plan."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="CASCADE"), index=True
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_results.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # screenshot|har|log|trace
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scrubbed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


def _embedding_column_type():
    """JSON by default; a real ``vector(n)`` when the deployment opted in.

    Decided once, here, because SQLAlchemy fixes a column's type when the model
    is defined. An earlier version of this tried to be clever - store JSON, then
    ``ALTER`` the column to ``vector`` at init when the extension turned out to
    be available. That fails in a way worth recording: the ALTER succeeds, and
    then every INSERT fails with "column is of type vector but expression is of
    type json", because the ORM is still binding the type it was defined with.
    A physical schema that disagrees with the mapper is not a graceful
    degradation, it is an outage.

    So it is a declared choice. ``QAGENT_VECTOR_BACKEND=pgvector`` requires both
    the package and the extension, and ``db_init`` checks for both and says so.
    The default requires neither and works on the image the compose file ships.
    """
    from qagent.config import get_settings

    settings = get_settings()
    if settings.qagent_vector_backend != "pgvector":
        return JSON

    try:
        from pgvector.sqlalchemy import Vector
    except ImportError as exc:  # pragma: no cover - configuration error path
        raise RuntimeError(
            "QAGENT_VECTOR_BACKEND=pgvector needs the pgvector package: "
            "pip install 'qagent[rag]'"
        ) from exc

    return Vector(settings.qagent_embedding_dimensions)


class CodeChunk(Base, TimestampMixin):
    """One indexed span of a project's source (CLAUDE.md sections 8 and 21).

    Persisted so an index survives the worker process that built it: without
    this, every scan re-chunks and re-embeds the whole checkout, which is free
    for BM25 and expensive for embeddings.
    """

    __tablename__ = "code_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Which checkout this was indexed from. Chunks from a stale commit are
    #: worse than none: they cite line numbers that have since moved.
    commit_sha: Mapped[str | None] = mapped_column(String(64), index=True)

    path: Mapped[str] = mapped_column(String(500), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(300))
    language: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: sha256 of (path, start_line, content). Lets a re-index skip text that has
    #: not changed, which is the whole point of persisting embeddings.
    digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    embedding: Mapped[list | None] = mapped_column(_embedding_column_type())
    embedding_model: Mapped[str | None] = mapped_column(String(100))

    __table_args__ = (
        Index("ix_code_chunks_project_digest", "project_id", "digest", unique=True),
    )


# --------------------------------------------------------------------------- defects


class Bug(Base, TimestampMixin):
    __tablename__ = "bugs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_results.id", ondelete="SET NULL")
    )

    reference: Mapped[str] = mapped_column(String(32), nullable=False)  # BUG-1042
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="severity"), default=Severity.MEDIUM, nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

    steps: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    root_cause: Mapped[str | None] = mapped_column(Text)
    suggested_fix: Mapped[str | None] = mapped_column(Text)
    reproduction_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)

    # Set when synced out to Jira / GitHub Issues (reference image 04).
    external_ref: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (UniqueConstraint("org_id", "reference", name="uq_bug_org_reference"),)


class BugEvent(Base, TimestampMixin):
    """One state change on a bug (CLAUDE.md section 20).

    The `Bug` row carries the *current* status and nothing else, which cannot
    answer the questions a defect's history is actually asked: when was this
    first seen, how long was it open, did it regress after being closed. A
    status column overwritten in place destroys exactly that.

    Deliberately append-only: nothing updates or deletes a row here. An audit
    trail that can be edited is not one.
    """

    __tablename__ = "bug_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bug_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: opened | reopened | status_changed | severity_changed | synced | closed
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    from_value: Mapped[str | None] = mapped_column(String(64))
    to_value: Mapped[str | None] = mapped_column(String(64))

    #: Null when QAgent itself made the change, which is the common case - a
    #: run reopening a defect has no human actor and pretending otherwise
    #: would attribute automated decisions to whoever last logged in.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="SET NULL")
    )
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (Index("ix_bug_events_bug_created", "bug_id", "created_at"),)


class BugComment(Base, TimestampMixin):
    """A human note on a defect (CLAUDE.md section 20)."""

    __tablename__ = "bug_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bug_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bugs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: True for a comment QAgent wrote (an AI analysis note), so a reader can
    #: tell a machine's opinion from a colleague's without checking the author.
    generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (Index("ix_bug_comments_bug_created", "bug_id", "created_at"),)


class Repository(Base, TimestampMixin):
    """A connected source repository (CLAUDE.md sections 5-6, 20).

    Separate from `Project` because the relationship is genuinely one-to-many
    in every case the plan describes: a project has a frontend repo and a
    backend repo, or a monorepo plus a deployment repo. Folding the URL into
    `Project` (where `repo_url` still lives, for the single-repo case) would
    make the second one a schema change.

    No token column, by design. ADR-0009 keeps credentials out of argv *and*
    out of the database; a connected repository holds a reference to a secret,
    never the secret.
    """

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    provider: Mapped[str] = mapped_column(String(32), default="github", nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(100), default="main", nullable=False)
    #: Where a checkout lives inside the repo, for a monorepo.
    subdirectory: Mapped[str | None] = mapped_column(String(300))
    #: Name of a secret in whatever store holds it - never the credential.
    secret_ref: Mapped[str | None] = mapped_column(String(200))

    last_analyzed_sha: Mapped[str | None] = mapped_column(String(64))
    last_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("project_id", "url", name="uq_repository_project_url"),
    )


class SecurityFinding(Base, TimestampMixin):
    """One static-analysis finding (CLAUDE.md section 16).

    QAgent doesn't reimplement a SAST engine, it shells out to one (Semgrep to
    start) and stores what it reported. The unique constraint is the dedupe key
    across repeated scans of the same repository: same rule, same location means
    the same finding, so re-scanning never doubles the count.
    """

    __tablename__ = "security_findings"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    tool: Mapped[str] = mapped_column(String(32), default="semgrep", nullable=False)
    rule_id: Mapped[str] = mapped_column(String(300), nullable=False)
    path: Mapped[str] = mapped_column(String(1000), nullable=False)
    line: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity, name="severity"), default=Severity.MEDIUM, nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(32))
    cwe: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    owasp: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="open", nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "project_id", "tool", "rule_id", "path", "line", name="uq_finding_location"
        ),
    )


class PerformanceRun(Base, TimestampMixin):
    """One k6 scenario at a fixed VU count (CLAUDE.md section 17).

    A load test is a sequence of these at rising VU levels, not a single row, so
    "where does it start degrading" is a query over this table rather than
    something computed once and thrown away.
    """

    __tablename__ = "performance_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    tool: Mapped[str] = mapped_column(String(32), default="k6", nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    vus: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_s: Mapped[float] = mapped_column(Float, nullable=False)

    requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    requests_per_s: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    failed_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_avg_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_p95_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_p99_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_max_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# --------------------------------------------------------------------------- ai layer


class AgentRun(Base, TimestampMixin):
    """One invocation of a named agent, with its full step trace.

    The trace is stored rather than a summary: when an agent misbehaves this is the
    only surface on which it can be debugged.
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("test_runs.id", ondelete="CASCADE"))

    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.PENDING, nullable=False
    )
    trace: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    calls: Mapped[list[LlmCall]] = relationship(
        back_populates="agent_run", cascade="all, delete-orphan"
    )


class LlmCall(Base, TimestampMixin):
    """Per-call cost accounting. Powers budgets, evals and future billing."""

    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    agent_run: Mapped[AgentRun] = relationship(back_populates="calls")


class QualityGate(Base, TimestampMixin):
    """One recorded deploy-or-block decision (CLAUDE.md sections 18, 20).

    `GET /projects/{id}/quality` and `qagent gate` both compute this verdict on
    demand, and a verdict that is only ever computed cannot be audited: nobody
    can answer "what did the gate say when we shipped the release that broke
    production", because the numbers it saw have since changed.

    So the decision is stored with the *inputs* it was made from, not just the
    outcome. Recomputing it later against today's open defects would produce a
    different, useless answer.
    """

    __tablename__ = "quality_gates"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("test_runs.id", ondelete="SET NULL"), index=True
    )

    result: Mapped[str] = mapped_column(String(16), nullable=False)  # pass | block | error
    reason: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    #: "ci" | "manual" | "api" - a gate run from a pull request and one run by
    #: hand mean different things when reading the history back.
    trigger: Mapped[str] = mapped_column(String(32), default="ci", nullable=False)

    #: The thresholds in force and the per-check numbers, exactly as
    #: modules/gate/policy.py produced them.
    policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    checks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (Index("ix_quality_gates_project_created", "project_id", "created_at"),)


class Deployment(Base, TimestampMixin):
    """A release the gate was consulted about (CLAUDE.md sections 18-19, 20).

    Recorded so the question that matters can be answered afterwards: did the
    defects QAgent found before a deploy correlate with what went wrong after
    it. Without a deployment row there is nothing to join a defect against but
    a timestamp.
    """

    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL")
    )
    quality_gate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("quality_gates.id", ondelete="SET NULL")
    )

    commit_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    version: Mapped[str | None] = mapped_column(String(100))
    #: pending | deployed | blocked | rolled_back
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_deployments_project_created", "project_id", "created_at"),)


class Notification(Base, TimestampMixin):
    """An outbound alert and whether it was actually delivered (§20, §23 Phase 5).

    Delivery status is stored rather than assumed. "We notified the team" is a
    claim a Slack outage silently falsifies, and a notification system that
    cannot tell you it failed is worse than none - it converts a loud problem
    into a quiet one.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    #: run_finished | gate_blocked | critical_defect | security_finding
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)  # slack | webhook | email
    target: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_notifications_status_created", "status", "created_at"),)


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource: Mapped[str] = mapped_column(String(200), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


#: Tables that carry org_id and therefore get row-level security policies.
TENANT_TABLES = [
    "users",
    "projects",
    "environments",
    "api_endpoints",
    "test_suites",
    "test_cases",
    "test_runs",
    "test_results",
    "artifacts",
    "code_chunks",
    "bugs",
    "bug_events",
    "bug_comments",
    "repositories",
    "quality_gates",
    "deployments",
    "notifications",
    "security_findings",
    "performance_runs",
    "agent_runs",
    "llm_calls",
    "audit_logs",
]
