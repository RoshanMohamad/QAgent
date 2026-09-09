"""FastAPI application.

One deployable service containing the modules from reference image 03, rather than
the twelve microservices that image draws. The module boundaries are real and the
seams are in place; splitting them across processes before there is load to justify
it buys distributed-systems failure modes and nothing else.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qagent import models
from qagent.config import get_settings
from qagent.db import get_db, set_tenant

logger = logging.getLogger(__name__)
settings = get_settings()

app = FastAPI(
    title="QAgent",
    version="0.1.0",
    description="Autonomous AI software quality engineering platform.",
)


# --------------------------------------------------------------------------- auth


def current_org(
    session: Session = Depends(get_db),
    x_org_id: str | None = Header(default=None, alias="X-Org-Id"),
) -> UUID:
    """Resolve the caller's organization and bind the transaction to it.

    MVP-level: the header stands in for a verified session or API key. It is
    deliberately the only place that decides tenancy, so replacing it with real
    authentication later touches exactly one function. Every query underneath is
    already constrained by RLS regardless of what application code does.
    """
    if not x_org_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "X-Org-Id header is required")
    try:
        org_id = UUID(x_org_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Org-Id must be a UUID") from exc

    set_tenant(session, org_id)
    return org_id


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


class RunIn(BaseModel):
    environment_id: UUID
    trigger: str = "manual"
    commit_sha: str | None = None


# ------------------------------------------------------------------------- routes


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "version": app.version, "env": settings.env}


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
    rows = session.execute(
        select(models.Bug)
        .where(models.Bug.project_id == project_id)
        .order_by(models.Bug.created_at.desc())
    ).scalars()

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
        }
        for b in rows
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
    blocking = counts.get("critical", 0) + counts.get("high", 0)

    return {
        "result": "block" if blocking else "pass",
        "reason": (
            f"{blocking} unresolved defect(s) at high or critical severity"
            if blocking
            else "no blocking defects"
        ),
        "run": {
            "id": str(latest.id),
            "total": latest.total,
            "passed": latest.passed,
            "failed": latest.failed,
        },
        "open_bugs": counts,
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

    spend = session.execute(select(func.coalesce(func.sum(models.LlmCall.usd), 0.0))).scalar_one()

    return {
        "projects": session.query(models.Project).count(),
        "runs": totals[0],
        "tests": {"total": totals[1], "passed": totals[2], "failed": totals[3]},
        "bugs": {(k.value if hasattr(k, "value") else str(k)): v for k, v in severities.items()},
        "llm_spend_usd": round(float(spend), 4),
        "generated_at": datetime.now(UTC),
    }
