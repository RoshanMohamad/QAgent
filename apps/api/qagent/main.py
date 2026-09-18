"""FastAPI application.

One deployable service containing the modules from reference image 03, rather than
the twelve microservices that image draws. The module boundaries are real and the
seams are in place; splitting them across processes before there is load to justify
it buys distributed-systems failure modes and nothing else.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
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
    hash_password,
    verify_password,
)
from qagent.persistence import persist_security_findings

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="QAgent",
    version="0.1.0",
    description="Autonomous AI software quality engineering platform.",
)

_ORG_SLUG_RE = re.compile(r"^[a-z0-9-]{2,100}$")


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


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - a JWT scheme name, not a credential
    org_id: str
    role: str


# ------------------------------------------------------------------------- routes


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "version": app.version, "env": settings.qagent_env}


@app.post("/api/v1/auth/register", status_code=201, tags=["auth"])
def register(payload: RegisterIn, session: Session = Depends(get_db)) -> TokenOut:
    """Create a new organization and its first (owner) user.

    Every user belongs to exactly one organization (see ADR on multi-tenancy in
    models.py), so signup and org creation are one step rather than two.
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


@app.post("/api/v1/auth/login", tags=["auth"])
def login(payload: LoginIn, session: Session = Depends(get_db)) -> TokenOut:
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
) -> dict:
    """Project Analyst agent (CLAUDE.md sections 6-8, agent 1): detect the stack
    and build the module tree from a checkout already on local disk.

    ``repo_path`` is a filesystem path the API process can read, the same
    contract ``security/scan`` uses. This only reads text -- no manifest is
    executed, no dependency installed -- so like the security scan it runs
    synchronously and needs none of the sandboxing section 22 requires for a
    live target. The result replaces ``Project.stack`` wholesale each run: it's
    a snapshot of the checkout as analyzed, not something to merge with history.
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


@app.post("/api/v1/projects/{project_id}/environments", status_code=201, tags=["projects"])
def create_environment(
    project_id: UUID,
    payload: EnvironmentIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
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


@app.post("/api/v1/projects/{project_id}/runs", status_code=202, tags=["runs"])
def start_run(
    project_id: UUID,
    payload: RunIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Queue a scan. Never executed inline: it talks to a third-party app over the network."""
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
    }


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
) -> dict:
    """Static analysis via Semgrep (CLAUDE.md section 16).

    ``repo_path`` is a filesystem path the API process can read - the same
    contract the CLI's ``--repo`` flags already use. Semgrep only parses source,
    it never executes it, so this runs synchronously and needs none of the
    sandboxing the runner/browser/explorer stages require for a live target.
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


@app.post("/api/v1/projects/{project_id}/performance/scan", status_code=202, tags=["performance"])
def start_performance_test(
    project_id: UUID,
    payload: PerformanceScanIn,
    org_id: UUID = Depends(current_org),
    session: Session = Depends(get_db),
) -> dict:
    """Load test via k6 (CLAUDE.md section 17): one scenario per VU level.

    Queued rather than run inline: unlike a security scan, this sends sustained
    real traffic to a live target for potentially minutes, which is exactly the
    kind of long, network-bound work that never belongs inside a request.
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

    return {
        "projects": session.query(models.Project).count(),
        "runs": totals[0],
        "tests": {"total": totals[1], "passed": totals[2], "failed": totals[3], "flaky": flaky},
        "bugs": {(k.value if hasattr(k, "value") else str(k)): v for k, v in severities.items()},
        "security_findings": {
            (k.value if hasattr(k, "value") else str(k)): v for k, v in security.items()
        },
        "llm_spend_usd": round(float(spend), 4),
        "generated_at": datetime.now(UTC),
    }
