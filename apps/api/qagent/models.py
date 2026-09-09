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


class EnvironmentMode(str, enum.Enum):
    """See ADR-0001. These are the only two supported input contracts."""

    TARGET_URL = "target_url"
    COMPOSE = "compose"


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"


class ResultStatus(str, enum.Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FailureClass(str, enum.Enum):
    """CLAUDE.md section 14. A failing test is not automatically a defect."""

    REAL_BUG = "real_bug"
    FLAKY_TEST = "flaky_test"
    ENVIRONMENT = "environment"
    NETWORK = "network"
    DEPENDENCY = "dependency"
    TEST_DATA = "test_data"
    BAD_ASSERTION = "bad_assertion"
    UNKNOWN = "unknown"


class TestKind(str, enum.Enum):
    API_FUNCTIONAL = "api_functional"
    API_SECURITY = "api_security"
    E2E = "e2e"


# --------------------------------------------------------------------------- tenancy


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    projects: Mapped[list[Project]] = relationship(back_populates="organization")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500))
    default_branch: Mapped[str] = mapped_column(String(100), default="main", nullable=False)

    # Output of the Project Analyst agent (CLAUDE.md section 8, agent 1).
    stack: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="projects")
    environments: Mapped[list[Environment]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Environment(Base, TimestampMixin):
    """A place tests can run against. See ADR-0001."""

    __tablename__ = "environments"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    # Non-secret request defaults. Credentials live behind secret_ref, never here.
    default_headers: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(String(200))

    project: Mapped[Project] = relationship(back_populates="environments")


class ApiEndpoint(Base, TimestampMixin):
    """One discovered operation. Populated by modules/discovery."""

    __tablename__ = "api_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    results: Mapped[list[TestResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class TestResult(Base, TimestampMixin):
    __tablename__ = "test_results"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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


# --------------------------------------------------------------------------- defects


class Bug(Base, TimestampMixin):
    __tablename__ = "bugs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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


# --------------------------------------------------------------------------- ai layer


class AgentRun(Base, TimestampMixin):
    """One invocation of a named agent, with its full step trace.

    The trace is stored rather than a summary: when an agent misbehaves this is the
    only surface on which it can be debugged.
    """

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    "bugs",
    "agent_runs",
    "llm_calls",
    "audit_logs",
]
