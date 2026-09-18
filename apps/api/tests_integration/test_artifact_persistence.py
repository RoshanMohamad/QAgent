"""Evidence artifacts actually reach storage and the database (CLAUDE.md section
15): a browser check's screenshot and console log become real `Artifact` rows a
reader can fetch back, against a real Postgres and a real (tmp-directory)
storage backend - not just an unused table and a docstring.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from qagent import models
from qagent.db import set_tenant
from qagent.modules.storage.local import LocalArtifactStore
from qagent.persistence import persist_result
from qagent.pipeline import ArtifactBytes, CaseOutcome, PipelineResult


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


def _seed_project(session):
    org = models.Organization(id=uuid.uuid4(), name="artifact-org", slug="artifact-org")
    session.add(org)
    session.commit()

    set_tenant(session, org.id)
    project = models.Project(org_id=org.id, name="p")
    session.add(project)
    session.flush()
    run = models.TestRun(org_id=org.id, project_id=project.id, status=models.RunStatus.RUNNING)
    session.add(run)
    session.flush()
    return org, project, run


def test_persist_result_writes_artifact_rows_for_a_browser_bug(tmp_path, Session) -> None:
    session = Session()
    org, project, run = _seed_project(session)

    result = PipelineResult(base_url="http://x", started_at=datetime.now(UTC))
    result.outcomes.append(
        CaseOutcome(
            name="page loads: http://x/checkout",
            kind="e2e",
            endpoint_key=None,
            status="failed",
            duration_ms=10,
            request={"method": "GET", "path": "http://x/checkout"},
            response={"status": 500},
            assertions=[],
            bug={"title": "500 on checkout", "severity": "high"},
            artifacts=[
                ArtifactBytes(
                    kind="screenshot", content_type="image/png", extension=".png", data=b"fake-png"
                ),
                ArtifactBytes(
                    kind="log",
                    content_type="text/plain",
                    extension=".log",
                    data=b"Authorization: Bearer super-secret-token\nUncaught TypeError",
                ),
            ],
        )
    )

    store = LocalArtifactStore(tmp_path)
    persist_result(
        session, org_id=org.id, project_id=project.id, run=run, result=result, artifact_store=store
    )
    session.commit()

    artifacts = (
        session.query(models.Artifact).filter(models.Artifact.run_id == run.id).all()
    )
    assert {a.kind for a in artifacts} == {"screenshot", "log"}

    screenshot = next(a for a in artifacts if a.kind == "screenshot")
    assert screenshot.scrubbed is False
    assert screenshot.content_type == "image/png"
    assert store.read(screenshot.storage_key) == b"fake-png"

    log = next(a for a in artifacts if a.kind == "log")
    assert log.scrubbed is True
    stored_log = store.read(log.storage_key)
    assert b"super-secret-token" not in stored_log
    assert b"Uncaught TypeError" in stored_log

    session.close()


def test_persist_result_writes_no_artifacts_when_check_captured_none(tmp_path, Session) -> None:
    session = Session()
    org, project, run = _seed_project(session)

    result = PipelineResult(base_url="http://x", started_at=datetime.now(UTC))
    result.outcomes.append(
        CaseOutcome(
            name="page loads: http://x/",
            kind="e2e",
            endpoint_key=None,
            status="passed",
            duration_ms=10,
            request={"method": "GET", "path": "http://x/"},
            response={"status": 200},
            assertions=[],
        )
    )

    persist_result(
        session,
        org_id=org.id,
        project_id=project.id,
        run=run,
        result=result,
        artifact_store=LocalArtifactStore(tmp_path),
    )
    session.commit()

    assert session.query(models.Artifact).filter(models.Artifact.run_id == run.id).count() == 0
    session.close()
