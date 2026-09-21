"""FastAPI application.

One deployable service containing the modules from reference image 03, rather than
the twelve microservices that image draws. The module boundaries are real and the
seams are in place; splitting them across processes before there is load to justify
it buys distributed-systems failure modes and nothing else.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import redis as redis_lib
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qagent import models
from qagent.config import get_settings
from qagent.db import get_db, set_tenant
from qagent.modules.auth.security import (
    AuthError,
    create_access_token,
    decode_access_token,
    has_role,
    hash_password,
    verify_password,
)
from qagent.modules.observability import metrics
from qagent.modules.observability.tracing import setup_tracing
from qagent.modules.ratelimit.limiter import RateLimiter
from qagent.persistence import persist_security_findings

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="QAgent",
    version="0.1.0",
    description="Autonomous AI software quality engineering platform.",
)

# No-op unless QAGENT_TRACING_ENABLED is set, and it swallows its own failures:
# an observability dependency that can stop the API from serving is a liability
# (modules/observability/tracing.py).
_tracing_active = setup_tracing(app=app)

_ORG_SLUG_RE = re.compile(r"^[a-z0-9-]{2,100}$")


@app.middleware("http")
async def _observe_requests(request: Request, call_next):
    """Every request, timed and counted (CLAUDE.md section 23).

    Runs before routing decides which endpoint handles the request, so the
    route template is read from ``request.scope`` *after* ``call_next``
    returns - Starlette fills it in once the route matches, and reading it any
    earlier would see nothing. ``/metrics`` and ``/health`` are excluded: a
    scraper hitting ``/metrics`` every 15s would otherwise show up in its own
    output, which answers a question nobody asked.
    """
    if request.url.path in {"/metrics", "/health"}:
        return await call_next(request)

    started = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - started

    route = request.scope.get("route")
    path_template = route.path if route is not None else request.url.path
    metrics.HTTP_REQUESTS.labels(
        method=request.method, path_template=path_template, status=response.status_code
    ).inc()
    metrics.HTTP_REQUEST_DURATION_SECONDS.labels(
        method=request.method, path_template=path_template
    ).observe(duration)
    return response


@app.get("/metrics", tags=["system"])
def get_metrics() -> Response:
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------- auth


@dataclass(frozen=True)
class Principal:
    org_id: UUID
    user_id: UUID
    role: str


def current_principal(
    session: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> Principal:
    """Verify the bearer token and bind the transaction to its organization.

    Replaces the earlier X-Org-Id header, which asked callers to self-report
    their tenant. This is the only place that decides identity and tenancy, so
    every route downstream inherits it through `current_org`/`current_principal`
    and every query is additionally constrained by RLS regardless.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = decode_access_token(token, settings.qagent_secret_key)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    org_id = UUID(claims["org_id"])
    set_tenant(session, org_id)
    return Principal(org_id=org_id, user_id=UUID(claims["sub"]), role=claims["role"])


def current_org(principal: Principal = Depends(current_principal)) -> UUID:
    return principal.org_id


def require_owner(principal: Principal = Depends(current_principal)) -> Principal:
    """Gate for the handful of actions CLAUDE.md section 22/23's RBAC asks for:
    reading an arbitrary local ``repo_path`` (analyze/security scan), sending
    real sustained traffic somewhere (performance scan), provisioning an
    environment's credentials, and managing who else is in the organization.
    Everything else stays reachable by any authenticated member - RLS already
    stops a member from touching another *organization's* data regardless of
    role, so this is about actions dangerous within one's own org, not tenancy.
    """
    if not has_role(principal.role, at_least="owner"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner role required")
    return principal


# --------------------------------------------------------------------- rate limiting

#: Lazy: redis-py doesn't open a connection until the first command, so this
#: never blocks import or app startup, and a Redis outage surfaces at request
#: time (caught below) rather than crashing the whole process on boot.
_redis_client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
_rate_limiter = RateLimiter(_redis_client)


def _too_many_requests(retry_after: int, detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, detail, headers={"Retry-After": str(retry_after)}
    )


def rate_limit_by_ip(bucket: str, *, limit: int, window_seconds: int):
    """Guards an unauthenticated endpoint (register/login) against brute-force
    and spam, keyed by the caller's address since there's no identity yet to
    key it by."""

    def dependency(request: Request) -> None:
        host = request.client.host if request.client else "unknown"
        try:
            result = _rate_limiter.hit(
                f"ratelimit:{bucket}:{host}", limit=limit, window_seconds=window_seconds
            )
        except redis_lib.RedisError:
            # Fails open, not closed: the budgets in modules/llm/budget.py make
            # the same call for the same reason (README Security section) - a
            # Redis outage should degrade a protection, not take auth down with it.
            logger.warning("rate limiter: redis unavailable, allowing request through")
            return
        if not result.allowed:
            raise _too_many_requests(result.retry_after_seconds, "rate limit exceeded")

    return dependency


def rate_limit_by_org(bucket: str, *, limit: int, window_seconds: int):
    """Guards an expensive, queued action per *tenant*, so one organization
    saturating the shared Celery queue can't starve every other one - RLS
    isolates data, not throughput."""

    def dependency(org_id: UUID = Depends(current_org)) -> None:
        try:
            result = _rate_limiter.hit(
                f"ratelimit:{bucket}:{org_id}", limit=limit, window_seconds=window_seconds
            )
        except redis_lib.RedisError:
            logger.warning("rate limiter: redis unavailable, allowing request through")
            return
        if not result.allowed:
            raise _too_many_requests(
                result.retry_after_seconds, "rate limit exceeded for this organization"
            )

    return dependency


# ------------------------------------------------------------------------ schemas


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    repo_url: str | None = None


class EnvironmentIn(BaseModel):
    name: str = "default"
    mode: models.EnvironmentMode = models.EnvironmentMode.TARGET_URL
    base_url: str
    openapi_url: str | None = None
    default_headers: dict[str, str] = Field(default_factory=dict)
    e2e_enabled: bool = False
    interactive_exploration_enabled: bool = False


class RunIn(BaseModel):
    environment_id: UUID
    trigger: str = "manual"
    commit_sha: str | None = None


class SecurityScanIn(BaseModel):
    repo_path: str = Field(min_length=1)
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)


class AnalyzeRepoIn(BaseModel):
    repo_path: str = Field(min_length=1)


class ConnectRepoIn(BaseModel):
    """Connect a GitHub repository (CLAUDE.md section 6).

    ``token`` is a PAT used for the clone and nothing else: it is never written
    to the database and never logged, and the request model excludes it from
    ``repr`` so it cannot reach a traceback or a debug dump. Omitted, the server
    falls back to ``$GITHUB_TOKEN`` - the same contract ``qagent report-issues``
    already uses - and a public repository needs neither.
    """

    repo_url: str = Field(min_length=1, max_length=500)
    branch: str | None = None
    token: str | None = Field(default=None, repr=False, exclude=True)


class BugCommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=10_000)


class GateRecordIn(BaseModel):
    """A gate decision made elsewhere - typically by `qagent gate` in CI.

    The decision is recorded with the policy and per-check numbers it was made
    from, because recomputing it later against today's open defects gives a
    different and useless answer (models.QualityGate).
    """

    run_id: UUID | None = None
    result: str = Field(pattern="^(pass|block|error)$")
    reason: str | None = None
    commit_sha: str | None = Field(default=None, max_length=64)
    trigger: str = Field(default="ci", max_length=32)
    policy: dict = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list)


class DeploymentIn(BaseModel):
    environment_id: UUID | None = None
    quality_gate_id: UUID | None = None
    commit_sha: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=100)
    status: str = Field(default="pending", pattern="^(pending|deployed|blocked|rolled_back)$")


class NotificationIn(BaseModel):
    event: str = Field(min_length=1, max_length=64)
    channel: str = Field(pattern="^(slack|webhook)$")
    target: str = Field(min_length=1, max_length=500)
    data: dict = Field(default_factory=dict)


class PerformanceScanIn(BaseModel):
    base_url: str = Field(min_length=1)
    vus_levels: list[int] = Field(default_factory=lambda: [100, 500, 1000, 5000])
    duration_seconds: float = Field(default=30.0, gt=0, le=600)
    paths: list[str] = Field(default_factory=lambda: ["/"])
    max_failed_rate: float = Field(default=0.01, ge=0, le=1)
    max_p95_ms: float = Field(default=1000.0, gt=0)


class RegisterIn(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    org_slug: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)


class LoginIn(BaseModel):
    org_slug: str
    email: str
    password: str


class InviteUserIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    role: str = "member"


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - a JWT scheme name, not a credential
    org_id: str
    role: str


# ------------------------------------------------------------------------- routes


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "version": app.version, "env": settings.qagent_env}


@app.post(
    "/api/v1/auth/register",
    status_code=201,
    tags=["auth"],
    dependencies=[Depends(rate_limit_by_ip("register", limit=5, window_seconds=60))],
)
def register(payload: RegisterIn, session: Session = Depends(get_db)) -> TokenOut:
    """Create a new organization and its first (owner) user.

    Every user belongs to exactly one organization (see ADR on multi-tenancy in
    models.py), so signup and org creation are one step rather than two.
    Rate-limited by IP (5/min): an unauthenticated endpoint that fills a table.
    """
    if not _ORG_SLUG_RE.match(payload.org_slug):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "org_slug must be 2-100 lowercase letters, digits or hyphens",
        )

    existing = session.execute(
        select(models.Organization).where(models.Organization.slug == payload.org_slug)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "organization slug already taken")

    org = models.Organization(name=payload.org_name, slug=payload.org_slug)
    session.add(org)
    session.flush()  # assigns org.id; organizations carries no org_id so no tenant guard needed yet

    set_tenant(session, org.id)
    user = models.User(
        org_id=org.id,
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        role="owner",
    )
    session.add(user)
    session.commit()

    token = create_access_token(
        user_id=user.id, org_id=org.id, role=user.role, secret_key=settings.qagent_secret_key
    )
    return TokenOut(access_token=token, org_id=str(org.id), role=user.role)


@app.post(
    "/api/v1/auth/login",
    tags=["auth"],
    dependencies=[Depends(rate_limit_by_ip("login", limit=10, window_seconds=60))],
)
def login(payload: LoginIn, session: Session = Depends(get_db)) -> TokenOut:
    """Rate-limited by IP (10/min): a password-guessing oracle otherwise."""
    org = session.execute(
        select(models.Organization).where(models.Organization.slug == payload.org_slug)
    ).scalar_one_or_none()

    # Same error for "no such org" and "wrong password" below: distinguishing them
    # would let a caller enumerate valid org slugs and emails.
    if org is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    set_tenant(session, org.id)
    user = session.execute(
        select(models.User).where(models.User.email == payload.email.lower())
    ).scalar_one_or_none()

    if user is None or not user.is_active or not verify_password(
        payload.password, user.hashed_password
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")

    token = create_access_token(
        user_id=user.id, org_id=org.id, role=user.role, secret_key=settings.qagent_secret_key
    )
    return TokenOut(access_token=token, org_id=str(org.id), role=user.role)


@app.post("/api/v1/users", status_code=201, tags=["auth"])
def invite_user(
    payload: InviteUserIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Add another user to the caller's organization. Owner-only (RBAC,
    CLAUDE.md section 23) - membership itself is the privilege being managed.

    No invite-token/email flow: an owner sets the new user's password directly,
    the same way `register` sets the first one. Layering email delivery on top
    is additive whenever there's a mail sender to layer it onto; the role check
    and the tenant-scoped uniqueness constraint (`uq_users_org_email`) are the
    part that actually matters for RBAC and are already enforced.
    """
    if payload.role not in ("member", "owner"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "role must be 'member' or 'owner'")

    existing = session.execute(
        select(models.User).where(models.User.email == payload.email.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "a user with this email already exists")

    user = models.User(
        org_id=org_id,
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    session.add(user)
    session.commit()
    return {"id": str(user.id), "email": user.email, "role": user.role}


@app.get("/api/v1/users", tags=["auth"])
def list_users(
    org_id: UUID = Depends(current_org), session: Session = Depends(get_db)
) -> list[dict]:
    """Any authenticated member can see who else is in their own organization -
    RLS already confines this to one org's rows regardless of role."""
    rows = session.execute(
        select(models.User).where(models.User.org_id == org_id).order_by(models.User.created_at)
    ).scalars()
    return [
        {"id": str(u.id), "email": u.email, "role": u.role, "is_active": u.is_active}
        for u in rows
    ]


@app.post("/api/v1/projects", status_code=201, tags=["projects"])
def create_project(
    payload: ProjectIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    project = models.Project(org_id=org_id, name=payload.name, repo_url=payload.repo_url)
    session.add(project)
    session.commit()
    return {"id": str(project.id), "name": project.name}


@app.get("/api/v1/projects", tags=["projects"])
def list_projects(
    org_id: UUID = Depends(current_org), session: Session = Depends(get_db)
) -> list[dict]:
    rows = session.execute(
        select(models.Project).order_by(models.Project.created_at.desc())
    ).scalars()
    return [
        {"id": str(p.id), "name": p.name, "repo_url": p.repo_url, "stack": p.stack} for p in rows
    ]


@app.post("/api/v1/projects/{project_id}/analyze", tags=["projects"])
def analyze_project(
    project_id: UUID,
    payload: AnalyzeRepoIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Project Analyst agent (CLAUDE.md sections 6-8, agent 1): detect the stack
    and build the module tree from a checkout already on local disk.

    ``repo_path`` is a filesystem path the API process can read, the same
    contract ``security/scan`` uses. This only reads text -- no manifest is
    executed, no dependency installed -- so like the security scan it runs
    synchronously and needs none of the sandboxing section 22 requires for a
    live target. The result replaces ``Project.stack`` wholesale each run: it's
    a snapshot of the checkout as analyzed, not something to merge with history.
    Owner-only (RBAC, CLAUDE.md section 23): it reads an arbitrary local path
    the API process can see, which is not a member-level action.
    """
    from qagent.modules.analyzer.analyst import analyze_repository

    project = session.get(models.Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    repo_dir = Path(payload.repo_path)
    if not repo_dir.is_dir():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"not a directory: {repo_dir}")

    analysis = analyze_repository(repo_dir)
    project.stack = analysis.to_dict()
    session.commit()

    return {"id": str(project.id), "stack": project.stack}


@app.post("/api/v1/projects/{project_id}/connect", tags=["projects"])
def connect_repository(
    project_id: UUID,
    payload: ConnectRepoIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Clone a GitHub repository and run the Project Analyst over it.

    ``analyze`` above requires the checkout to already be on a disk the API can
    read, which makes "connect a repository" a manual step the operator does
    first. This does that step: shallow-clone into a temporary directory, run
    the identical ``analyze_repository`` (one analyzer, not two), persist the
    result plus the repository it came from, and delete the checkout - it is
    never kept, because nothing downstream reads source after analysis, and a
    retained third-party checkout is standing risk for no benefit (ADR-0004).

    Owner-only for the same reasons ``analyze`` is, plus one more: it makes the
    server open an outbound connection and may carry a credential.
    """
    import os

    from qagent.modules.analyzer.analyst import analyze_repository
    from qagent.modules.provisioning.clone import (
        CloneError,
        cloned_repository,
        head_commit,
        parse_repo_url,
    )

    project = session.get(models.Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    token = payload.token or os.environ.get("GITHUB_TOKEN") or None

    try:
        ref = parse_repo_url(payload.repo_url)
        with cloned_repository(payload.repo_url, branch=payload.branch, token=token) as repo_dir:
            analysis = analyze_repository(repo_dir)
            commit_sha = head_commit(repo_dir)
    except CloneError as exc:
        # 422, not 500: every CloneError is something about the caller's
        # request - the URL, the branch, or the credential - not a server fault.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    project.repo_url = ref.https_url
    if payload.branch:
        project.default_branch = payload.branch
    project.stack = analysis.to_dict()
    session.commit()

    return {
        "id": str(project.id),
        "repo_url": project.repo_url,
        "branch": project.default_branch,
        "commit_sha": commit_sha,
        "summary": analysis.summary(),
        "stack": project.stack,
    }


@app.post("/api/v1/projects/{project_id}/environments", status_code=201, tags=["projects"])
def create_environment(
    project_id: UUID,
    payload: EnvironmentIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Owner-only (RBAC): an environment carries where a scan sends real
    traffic and, eventually, `secret_ref` - provisioning that is not a
    member-level action."""
    if session.get(models.Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    environment = models.Environment(
        org_id=org_id,
        project_id=project_id,
        name=payload.name,
        mode=payload.mode,
        base_url=payload.base_url,
        openapi_url=payload.openapi_url,
        default_headers=payload.default_headers,
        e2e_enabled=payload.e2e_enabled,
        interactive_exploration_enabled=payload.interactive_exploration_enabled,
    )
    session.add(environment)
    session.commit()
    return {"id": str(environment.id), "base_url": environment.base_url}


@app.post(
    "/api/v1/projects/{project_id}/runs",
    status_code=202,
    tags=["runs"],
    dependencies=[Depends(rate_limit_by_org("runs", limit=30, window_seconds=60))],
)
def start_run(
    project_id: UUID,
    payload: RunIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Queue a scan. Never executed inline: it talks to a third-party app over
    the network. Rate-limited per organization (30/min): nothing else stops one
    tenant from saturating the shared Celery queue every other tenant waits on.
    """
    if session.get(models.Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    run = models.TestRun(
        org_id=org_id,
        project_id=project_id,
        environment_id=payload.environment_id,
        trigger=payload.trigger,
        commit_sha=payload.commit_sha,
        status=models.RunStatus.PENDING,
    )
    session.add(run)
    session.commit()

    from qagent.worker.tasks import run_scan

    run_scan.delay(str(org_id), str(project_id), str(run.id))
    return {"id": str(run.id), "status": run.status.value}


@app.get("/api/v1/runs/{run_id}", tags=["runs"])
def get_run(
    run_id: UUID, org_id: UUID = Depends(current_org), session: Session = Depends(get_db)
) -> dict:
    run = session.get(models.TestRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    classifications = dict(
        session.execute(
            select(models.TestResult.failure_class, func.count())
            .where(models.TestResult.run_id == run_id, models.TestResult.failure_class.isnot(None))
            .group_by(models.TestResult.failure_class)
        ).all()
    )

    return {
        "id": str(run.id),
        "status": run.status.value,
        "total": run.total,
        "passed": run.passed,
        "failed": run.failed,
        "errored": run.errored,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "classifications": {
            (k.value if hasattr(k, "value") else str(k)): v for k, v in classifications.items()
        },
        # The plan itself can be long; the run view wants the shape, not every
        # check. `GET /runs/{id}/plan` returns the full document.
        "plan": (run.plan or {}).get("summary"),
        "coverage": run.coverage or {},
    }


@app.get("/api/v1/runs/{run_id}/plan", tags=["runs"])
def get_run_plan(
    run_id: UUID, org_id: UUID = Depends(current_org), session: Session = Depends(get_db)
) -> dict:
    """The full test strategy this run was held to (agent 2, CLAUDE.md section 8).

    Separate from the run view because it answers a different question: not
    "what happened" but "what was this run supposed to cover, and what did it
    leave out". A run that passed every check it ran while skipping the auth
    module is not a green run, and this is where that shows.
    """
    run = session.get(models.TestRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    return {"run_id": str(run.id), "plan": run.plan or {}, "coverage": run.coverage or {}}


@app.get("/api/v1/runs/{run_id}/results", tags=["runs"])
def get_results(
    run_id: UUID,
    only_failed: bool = True,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    query = (
        select(models.TestResult, models.TestCase.name)
        .join(models.TestCase, models.TestCase.id == models.TestResult.test_case_id)
        .where(models.TestResult.run_id == run_id)
    )
    if only_failed:
        query = query.where(models.TestResult.status != models.ResultStatus.PASSED)

    return [
        {
            "id": str(result.id),
            "name": name,
            "status": result.status.value,
            "duration_ms": result.duration_ms,
            "failure_class": result.failure_class.value if result.failure_class else None,
            "confidence": result.triage_confidence,
            "failure_message": result.failure_message,
            "assertions": result.assertions,
        }
        for result, name in session.execute(query).all()
    ]


@app.get("/api/v1/projects/{project_id}/bugs", tags=["bugs"])
def list_bugs(
    project_id: UUID,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    bugs = list(
        session.execute(
            select(models.Bug)
            .where(models.Bug.project_id == project_id)
            .order_by(models.Bug.created_at.desc())
        ).scalars()
    )

    result_ids = [b.result_id for b in bugs if b.result_id is not None]
    artifacts_by_result: dict[UUID, list[models.Artifact]] = {}
    if result_ids:
        for artifact in session.execute(
            select(models.Artifact).where(models.Artifact.result_id.in_(result_ids))
        ).scalars():
            artifacts_by_result.setdefault(artifact.result_id, []).append(artifact)

    return [
        {
            "reference": b.reference,
            "title": b.title,
            "severity": b.severity.value,
            "status": b.status,
            "expected": b.expected,
            "actual": b.actual,
            "root_cause": b.root_cause,
            "suggested_fix": b.suggested_fix,
            "steps": b.steps,
            # Evidence (CLAUDE.md section 15): a screenshot/log a reader can
            # actually open, not just a claim in `actual`. Fetch the bytes at
            # GET /api/v1/artifacts/{id}.
            "artifacts": [
                {"id": str(a.id), "kind": a.kind, "content_type": a.content_type}
                for a in artifacts_by_result.get(b.result_id, [])
            ],
        }
        for b in bugs
    ]


@app.get("/api/v1/bugs/{bug_id}/history", tags=["bugs"])
def bug_history(
    bug_id: UUID, org_id: UUID = Depends(current_org), session: Session = Depends(get_db)
) -> dict:
    """A defect's lifecycle: when it opened, reproduced, changed or regressed.

    The `Bug` row carries only the current status, which cannot answer "how long
    has this been open" or "did it come back after we closed it". This can.
    """
    bug = session.get(models.Bug, bug_id)
    if bug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bug not found")

    events = session.execute(
        select(models.BugEvent)
        .where(models.BugEvent.bug_id == bug_id)
        .order_by(models.BugEvent.created_at)
    ).scalars().all()

    comments = session.execute(
        select(models.BugComment)
        .where(models.BugComment.bug_id == bug_id)
        .order_by(models.BugComment.created_at)
    ).scalars().all()

    return {
        "bug": {
            "id": str(bug.id),
            "reference": bug.reference,
            "title": bug.title,
            "severity": bug.severity.value,
            "status": bug.status,
            "first_seen": bug.created_at,
        },
        "events": [
            {
                "event": e.event,
                "from": e.from_value,
                "to": e.to_value,
                "run_id": str(e.run_id) if e.run_id else None,
                "at": e.created_at,
            }
            for e in events
        ],
        "comments": [
            {
                "id": str(c.id),
                "body": c.body,
                "generated": c.generated,
                "author_user_id": str(c.author_user_id) if c.author_user_id else None,
                "at": c.created_at,
            }
            for c in comments
        ],
        # The number a triage meeting actually asks for.
        "reopen_count": sum(1 for e in events if e.event == "reopened"),
    }


@app.post("/api/v1/bugs/{bug_id}/comments", status_code=201, tags=["bugs"])
def add_bug_comment(
    bug_id: UUID,
    payload: BugCommentIn,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_db),
) -> dict:
    bug = session.get(models.Bug, bug_id)
    if bug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bug not found")

    comment = models.BugComment(
        org_id=principal.org_id,
        bug_id=bug_id,
        author_user_id=principal.user_id,
        body=payload.body,
        generated=False,
    )
    session.add(comment)
    session.commit()
    return {"id": str(comment.id), "at": comment.created_at}


@app.post("/api/v1/projects/{project_id}/gates", status_code=201, tags=["quality"])
def record_gate(
    project_id: UUID,
    payload: GateRecordIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Record a gate decision made in CI, with the inputs it was made from.

    `GET /projects/{id}/quality` computes a verdict on demand; this stores one.
    Both are needed: the computed one gates the deploy, the stored one answers
    "what did the gate say when we shipped the release that broke production".
    """
    gate = models.QualityGate(
        org_id=org_id,
        project_id=project_id,
        run_id=payload.run_id,
        result=payload.result,
        reason=payload.reason,
        commit_sha=payload.commit_sha,
        trigger=payload.trigger,
        policy=payload.policy,
        checks=payload.checks,
    )
    session.add(gate)
    session.commit()
    return {"id": str(gate.id), "result": gate.result, "at": gate.created_at}


@app.get("/api/v1/projects/{project_id}/gates", tags=["quality"])
def list_gates(
    project_id: UUID,
    limit: int = 20,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    rows = session.execute(
        select(models.QualityGate)
        .where(models.QualityGate.project_id == project_id)
        .order_by(models.QualityGate.created_at.desc())
        .limit(min(limit, 100))
    ).scalars().all()

    return [
        {
            "id": str(g.id),
            "result": g.result,
            "reason": g.reason,
            "commit_sha": g.commit_sha,
            "trigger": g.trigger,
            "checks": g.checks,
            "at": g.created_at,
        }
        for g in rows
    ]


@app.post("/api/v1/projects/{project_id}/deployments", status_code=201, tags=["quality"])
def record_deployment(
    project_id: UUID,
    payload: DeploymentIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Record a release, so defects can later be correlated with what broke."""
    deployment = models.Deployment(
        org_id=org_id,
        project_id=project_id,
        environment_id=payload.environment_id,
        quality_gate_id=payload.quality_gate_id,
        commit_sha=payload.commit_sha,
        version=payload.version,
        status=payload.status,
        deployed_at=datetime.now(UTC) if payload.status == "deployed" else None,
    )
    session.add(deployment)
    session.commit()
    return {"id": str(deployment.id), "status": deployment.status}


@app.post("/api/v1/projects/{project_id}/notifications", status_code=202, tags=["notifications"])
def send_notification(
    project_id: UUID,
    payload: NotificationIn,
    principal: Principal = Depends(require_owner),
    session: Session = Depends(get_db),
) -> dict:
    """Send an alert and record whether it actually arrived.

    Owner-gated because the target is a user-supplied URL that this server will
    then fetch - the same reason `analyze` and `performance/scan` are gated. The
    egress guard refuses link-local and private addresses regardless.

    Returns 202 with the delivery status rather than failing on a dead webhook:
    the notification is recorded either way, and a caller that needs to know can
    read `delivered`.
    """
    from qagent.modules.notify.dispatch import render, send, slack_payload, webhook_payload

    title, lines = render(payload.event, payload.data)
    body = (
        slack_payload(event=payload.event, title=title, lines=lines)
        if payload.channel == "slack"
        else webhook_payload(event=payload.event, title=title, lines=lines, data=payload.data)
    )

    notification = models.Notification(
        org_id=principal.org_id,
        project_id=project_id,
        event=payload.event,
        channel=payload.channel,
        target=payload.target,
        payload=body,
        status="pending",
    )
    session.add(notification)
    session.flush()

    result = send(
        channel=payload.channel,
        target=payload.target,
        payload=body,
        allow_private=not settings.is_production,
    )

    notification.attempts += 1
    notification.status = "delivered" if result.delivered else "failed"
    notification.last_error = result.error
    notification.delivered_at = result.delivered_at
    session.commit()

    return {
        "id": str(notification.id),
        "status": notification.status,
        "delivered": result.delivered,
        "error": result.error,
    }


@app.get("/api/v1/artifacts/{artifact_id}", tags=["bugs"])
def get_artifact(
    artifact_id: UUID,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> Response:
    """Fetch one piece of evidence's raw bytes (CLAUDE.md section 15).

    The `Artifact` row is filtered by RLS the same as everything else - a
    caller can only ever look up an id belonging to their own organization,
    404 either way, so this never distinguishes "not yours" from "not found."
    """
    from qagent.modules.storage.local import ArtifactNotFound, store_from_settings

    artifact = session.get(models.Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")

    try:
        data = store_from_settings().read(artifact.storage_key)
    except ArtifactNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found") from exc

    return Response(content=data, media_type=artifact.content_type)


@app.get("/api/v1/projects/{project_id}/tests/flaky", tags=["tests"])
def list_flaky_tests(
    project_id: UUID,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    """Cases flagged unreliable by qagent.modules.triage.flakiness.

    A case appears here because its flake_rate crossed the quarantine threshold,
    not because its latest run failed - see the module docstring for why the two
    are tracked separately.
    """
    rows = session.execute(
        select(models.TestCase, models.TestSuite.name)
        .join(models.TestSuite, models.TestSuite.id == models.TestCase.suite_id)
        .where(models.TestSuite.project_id == project_id, models.TestCase.quarantined.is_(True))
        .order_by(models.TestCase.flake_rate.desc())
    ).all()

    return [
        {
            "name": case.name,
            "suite": suite_name,
            "kind": case.kind.value,
            "flake_rate": round(case.flake_rate, 3),
            "enabled": case.enabled,
        }
        for case, suite_name in rows
    ]


@app.post("/api/v1/projects/{project_id}/security/scan", status_code=201, tags=["security"])
def run_security_scan(
    project_id: UUID,
    payload: SecurityScanIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Static analysis via Semgrep (CLAUDE.md section 16).

    ``repo_path`` is a filesystem path the API process can read - the same
    contract the CLI's ``--repo`` flags already use. Semgrep only parses source,
    it never executes it, so this runs synchronously and needs none of the
    sandboxing the runner/browser/explorer stages require for a live target.
    Owner-only (RBAC): reads an arbitrary local path.
    """
    from qagent.modules.security.semgrep import SemgrepError, SemgrepUnavailable, run_semgrep

    if session.get(models.Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    try:
        result = run_semgrep(Path(payload.repo_path), timeout_seconds=payload.timeout_seconds)
    except SemgrepUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except SemgrepError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    inserted = persist_security_findings(
        session, org_id=org_id, project_id=project_id, findings=result.findings
    )
    session.commit()

    return {
        "findings": len(result.findings),
        "new_findings": inserted,
        "by_severity": result.counts_by_severity(),
        "scan_errors": result.scan_errors,
    }


@app.get("/api/v1/projects/{project_id}/security", tags=["security"])
def list_security_findings(
    project_id: UUID,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    rows = session.execute(
        select(models.SecurityFinding)
        .where(
            models.SecurityFinding.project_id == project_id,
            models.SecurityFinding.status == "open",
        )
        .order_by(models.SecurityFinding.severity.desc(), models.SecurityFinding.created_at.desc())
    ).scalars()

    return [
        {
            "tool": f.tool,
            "rule_id": f.rule_id,
            "title": f.title,
            "severity": f.severity.value,
            "path": f.path,
            "line": f.line,
            "message": f.message,
            "confidence": f.confidence,
            "cwe": f.cwe,
            "owasp": f.owasp,
        }
        for f in rows
    ]


@app.post(
    "/api/v1/projects/{project_id}/performance/scan",
    status_code=202,
    tags=["performance"],
    dependencies=[Depends(rate_limit_by_org("performance", limit=5, window_seconds=60))],
)
def start_performance_test(
    project_id: UUID,
    payload: PerformanceScanIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
    _owner: Principal = Depends(require_owner),
) -> dict:
    """Load test via k6 (CLAUDE.md section 17): one scenario per VU level.

    Queued rather than run inline: unlike a security scan, this sends sustained
    real traffic to a live target for potentially minutes, which is exactly the
    kind of long, network-bound work that never belongs inside a request.
    Owner-only (RBAC): sends real sustained traffic somewhere, at up to 5,000
    concurrent VUs by default (CLAUDE.md section 17) - not a member-level action.
    Rate-limited per organization (5/min) tighter than a regular scan, since
    each call can put sustained load on a real target for minutes.
    """
    if session.get(models.Project, project_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "project not found")

    from qagent.worker.tasks import run_performance_test

    run_performance_test.delay(
        str(org_id),
        str(project_id),
        payload.base_url,
        payload.vus_levels,
        payload.duration_seconds,
        payload.paths,
        payload.max_failed_rate,
        payload.max_p95_ms,
    )

    return {"status": "queued", "vus_levels": payload.vus_levels}


@app.get("/api/v1/projects/{project_id}/performance", tags=["performance"])
def list_performance_runs(
    project_id: UUID,
    limit: int = 20,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> list[dict]:
    rows = session.execute(
        select(models.PerformanceRun)
        .where(models.PerformanceRun.project_id == project_id)
        .order_by(models.PerformanceRun.created_at.desc(), models.PerformanceRun.vus.desc())
        .limit(limit)
    ).scalars()

    return [
        {
            "tool": r.tool,
            "base_url": r.base_url,
            "vus": r.vus,
            "duration_s": r.duration_s,
            "requests": r.requests,
            "requests_per_s": round(r.requests_per_s, 2),
            "failed_rate": round(r.failed_rate, 4),
            "latency_avg_ms": round(r.latency_avg_ms, 1),
            "latency_p95_ms": round(r.latency_p95_ms, 1),
            "latency_p99_ms": round(r.latency_p99_ms, 1),
            "latency_max_ms": round(r.latency_max_ms, 1),
            "passed": r.passed,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@app.get("/api/v1/projects/{project_id}/quality", tags=["quality"])
def quality_gate(
    project_id: UUID,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """The CI/CD decision (CLAUDE.md section 18).

    The gate blocks on *classified defects*, not on red tests. Blocking a deploy
    because the staging database was down is how a quality gate gets switched off
    permanently, so only failures triaged as real defects count against it.
    """
    latest = session.execute(
        select(models.TestRun)
        .where(models.TestRun.project_id == project_id)
        .order_by(models.TestRun.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    if latest is None:
        return {"result": "no_data", "reason": "no runs recorded for this project"}

    open_bugs = session.execute(
        select(models.Bug.severity, func.count())
        .where(models.Bug.project_id == project_id, models.Bug.status == "open")
        .group_by(models.Bug.severity)
    ).all()

    counts = {s.value if hasattr(s, "value") else str(s): c for s, c in open_bugs}
    blocking_bugs = counts.get("critical", 0) + counts.get("high", 0)

    open_findings = session.execute(
        select(models.SecurityFinding.severity, func.count())
        .where(
            models.SecurityFinding.project_id == project_id,
            models.SecurityFinding.status == "open",
        )
        .group_by(models.SecurityFinding.severity)
    ).all()

    finding_counts = {s.value if hasattr(s, "value") else str(s): c for s, c in open_findings}
    blocking_findings = finding_counts.get("critical", 0) + finding_counts.get("high", 0)

    # Performance runs have no "open/resolved" workflow like a bug does - they're an
    # immutable time series, so "blocking" only makes sense for the latest scan, not
    # for any failure ever recorded. Scenarios from the same scan share one
    # created_at (they're inserted in a single transaction; Postgres's now() is
    # transaction-stable), which is what "latest scan" means here.
    latest_perf_at = session.execute(
        select(func.max(models.PerformanceRun.created_at)).where(
            models.PerformanceRun.project_id == project_id
        )
    ).scalar_one_or_none()

    failing_scenarios = 0
    if latest_perf_at is not None:
        failing_scenarios = session.execute(
            select(func.count()).where(
                models.PerformanceRun.project_id == project_id,
                models.PerformanceRun.created_at == latest_perf_at,
                models.PerformanceRun.passed.is_(False),
            )
        ).scalar_one()

    blocking = blocking_bugs + blocking_findings + failing_scenarios

    reasons = []
    if blocking_bugs:
        reasons.append(f"{blocking_bugs} unresolved defect(s) at high or critical severity")
    if blocking_findings:
        reasons.append(
            f"{blocking_findings} unresolved security finding(s) at high or critical severity"
        )
    if failing_scenarios:
        reasons.append(f"{failing_scenarios} load test scenario(s) exceeded threshold")

    return {
        "result": "block" if blocking else "pass",
        "reason": "; ".join(reasons) if reasons else "no blocking defects",
        "run": {
            "id": str(latest.id),
            "total": latest.total,
            "passed": latest.passed,
            "failed": latest.failed,
        },
        "open_bugs": counts,
        "open_security_findings": finding_counts,
        "failing_performance_scenarios": failing_scenarios,
    }


@app.get("/api/v1/dashboard", tags=["dashboard"])
def dashboard(org_id: UUID = Depends(current_org), session: Session = Depends(get_db)) -> dict:
    """Counters for the dashboard in CLAUDE.md section 4."""
    totals = session.execute(
        select(
            func.count(models.TestRun.id),
            func.coalesce(func.sum(models.TestRun.total), 0),
            func.coalesce(func.sum(models.TestRun.passed), 0),
            func.coalesce(func.sum(models.TestRun.failed), 0),
        )
    ).one()

    severities = dict(
        session.execute(
            select(models.Bug.severity, func.count())
            .where(models.Bug.status == "open")
            .group_by(models.Bug.severity)
        ).all()
    )

    security = dict(
        session.execute(
            select(models.SecurityFinding.severity, func.count())
            .where(models.SecurityFinding.status == "open")
            .group_by(models.SecurityFinding.severity)
        ).all()
    )

    spend = session.execute(select(func.coalesce(func.sum(models.LlmCall.usd), 0.0))).scalar_one()
    flaky = session.query(models.TestCase).filter(models.TestCase.quarantined.is_(True)).count()

    # Surface coverage, from what was persisted rather than from one run: every
    # endpoint discovery has ever recorded, against those some test case is
    # linked to. See modules/coverage/surface.py for why this is not called
    # "backend coverage" - QAgent never instruments the application, so it
    # cannot report line coverage and must not appear to.
    endpoints_total = session.query(models.ApiEndpoint).count()
    endpoints_covered = session.execute(
        select(func.count(func.distinct(models.TestCase.endpoint_id))).where(
            models.TestCase.endpoint_id.isnot(None)
        )
    ).scalar_one()

    return {
        "projects": session.query(models.Project).count(),
        "runs": totals[0],
        "tests": {"total": totals[1], "passed": totals[2], "failed": totals[3], "flaky": flaky},
        "bugs": {(k.value if hasattr(k, "value") else str(k)): v for k, v in severities.items()},
        "security_findings": {
            (k.value if hasattr(k, "value") else str(k)): v for k, v in security.items()
        },
        "coverage": {
            "endpoints_total": endpoints_total,
            "endpoints_covered": endpoints_covered,
            "endpoint_percent": (
                100 if endpoints_total == 0 else round(100 * endpoints_covered / endpoints_total)
            ),
            "measures": (
                "Share of the discovered API surface that some test case exercises. "
                "Not line coverage: QAgent tests the application as a black box."
            ),
        },
        "llm_spend_usd": round(float(spend), 4),
        "generated_at": datetime.now(UTC),
    }


@app.get("/api/v1/usage", tags=["usage"])
def usage(
    since: datetime | None = None,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Per-organization usage for a billing period (CLAUDE.md section 23).

    Distinct from `/dashboard`'s all-time `llm_spend_usd`: this is windowed,
    which is the only shape "billing/usage" actually means - "spend since ever"
    answers nothing a plan or an invoice needs. Defaults to the current UTC
    calendar month, since that's the period every eventual billing cycle
    described in CLAUDE.md section 23 would run against.

    This computes the numbers a billing system would meter against; it does
    not itself bill anyone. Wiring a real invoice or payment provider on top
    is additive whenever there's a provider to wire - see ADR-0008.
    """
    period_start = since or datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    runs_by_status = dict(
        session.execute(
            select(models.TestRun.status, func.count())
            .where(models.TestRun.created_at >= period_start)
            .group_by(models.TestRun.status)
        ).all()
    )

    llm = session.execute(
        select(
            func.count(models.LlmCall.id),
            func.coalesce(func.sum(models.LlmCall.input_tokens + models.LlmCall.output_tokens), 0),
            func.coalesce(func.sum(models.LlmCall.usd), 0.0),
        ).where(models.LlmCall.created_at >= period_start)
    ).one()

    defects_by_severity = dict(
        session.execute(
            select(models.Bug.severity, func.count())
            .where(models.Bug.created_at >= period_start)
            .group_by(models.Bug.severity)
        ).all()
    )

    return {
        "org_id": str(org_id),
        "period_start": period_start,
        "generated_at": datetime.now(UTC),
        "runs": {
            (k.value if hasattr(k, "value") else str(k)): v for k, v in runs_by_status.items()
        },
        "llm": {"calls": llm[0], "tokens": llm[1], "spend_usd": round(float(llm[2]), 4)},
        "defects": {
            (k.value if hasattr(k, "value") else str(k)): v
            for k, v in defects_by_severity.items()
        },
    }
