"""Defect identity across runs, against a real Postgres.

The behaviour under test is a fix, not a feature. Persistence used to insert a
`Bug` row unconditionally for every reported defect, so a defect that survived
ten runs became ten `BUG-` references and the dashboard counted it ten times.
Now the same test case maps to one defect, and `bug_events` records what
happened to it - which is what makes "how long has this been open" and "did it
come back after we closed it" answerable at all.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from qagent import models
from qagent.db import set_tenant
from qagent.modules.storage.local import LocalArtifactStore
from qagent.persistence import persist_result
from qagent.pipeline import CaseOutcome, PipelineResult


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(tmp_path / "artifacts")


def _seed(session):
    org = models.Organization(id=uuid.uuid4(), name="o", slug=f"bug-{uuid.uuid4().hex[:8]}")
    session.add(org)
    session.commit()

    set_tenant(session, org.id)
    project = models.Project(org_id=org.id, name="p")
    session.add(project)
    session.flush()
    return org, project


def _outcome(severity: str = "high", name: str = "GET /products/{id} rejects a malformed id"):
    return CaseOutcome(
        name=name,
        kind="api_functional",
        endpoint_key="GET /products/{id}",
        status="failed",
        duration_ms=5,
        request={"method": "GET", "path": "/products/{id}"},
        response={"status": 500},
        assertions=[],
        verdict={"failure_class": "real_bug", "confidence": 0.9},
        bug={
            "title": "Identifier cast without validation",
            "severity": severity,
            "steps": ["send a malformed id"],
            "expected": "400",
            "actual": "500",
            "root_cause": "int() without a guard",
            "suggested_fix": "validate before casting",
        },
    )


def _scan(session, org, project, store, outcome) -> models.TestRun:
    result = PipelineResult(base_url="http://app", started_at=datetime.now(UTC))
    result.outcomes = [outcome]
    result.finished_at = datetime.now(UTC)

    run = models.TestRun(org_id=org.id, project_id=project.id, status=models.RunStatus.RUNNING)
    session.add(run)
    session.flush()
    persist_result(
        session,
        org_id=org.id,
        project_id=project.id,
        run=run,
        result=result,
        artifact_store=store,
    )
    session.commit()
    # SET LOCAL dies with the transaction (db.py, ADR-0007).
    set_tenant(session, org.id)
    return run


def _events(session) -> list[str]:
    return [
        e.event
        for e in session.execute(
            select(models.BugEvent).order_by(models.BugEvent.created_at)
        ).scalars()
    ]


def test_a_first_defect_opens_one_bug(Session, store) -> None:
    with Session() as session:
        org, project = _seed(session)

        _scan(session, org, project, store, _outcome())

        assert session.query(models.Bug).count() == 1
        assert _events(session) == ["opened"]


def test_the_same_defect_twice_is_one_bug(Session, store) -> None:
    """Ten runs used to mean ten BUG- references for one defect."""
    with Session() as session:
        org, project = _seed(session)

        _scan(session, org, project, store, _outcome())
        _scan(session, org, project, store, _outcome())
        _scan(session, org, project, store, _outcome())

        assert session.query(models.Bug).count() == 1
        assert _events(session) == ["opened", "reproduced", "reproduced"]


def test_a_defect_that_returns_after_closing_is_recorded_as_a_regression(Session, store) -> None:
    with Session() as session:
        org, project = _seed(session)
        _scan(session, org, project, store, _outcome())

        bug = session.query(models.Bug).one()
        bug.status = "closed"
        session.commit()
        set_tenant(session, org.id)

        _scan(session, org, project, store, _outcome())

        bug = session.query(models.Bug).one()
        assert bug.status == "open"
        assert "reopened" in _events(session)

        reopened = session.execute(
            select(models.BugEvent).where(models.BugEvent.event == "reopened")
        ).scalar_one()
        assert reopened.from_value == "closed"
        assert reopened.to_value == "open"


def test_a_severity_change_is_recorded(Session, store) -> None:
    with Session() as session:
        org, project = _seed(session)

        _scan(session, org, project, store, _outcome(severity="high"))
        _scan(session, org, project, store, _outcome(severity="critical"))

        assert session.query(models.Bug).one().severity is models.Severity.CRITICAL
        change = session.execute(
            select(models.BugEvent).where(models.BugEvent.event == "severity_changed")
        ).scalar_one()
        assert change.from_value == "high"
        assert change.to_value == "critical"


def test_two_different_defects_stay_separate(Session, store) -> None:
    with Session() as session:
        org, project = _seed(session)

        _scan(session, org, project, store, _outcome(name="check one"))
        _scan(session, org, project, store, _outcome(name="check two"))

        assert session.query(models.Bug).count() == 2
        references = {b.reference for b in session.query(models.Bug).all()}
        assert len(references) == 2


def test_the_defect_points_at_the_newest_evidence(Session, store) -> None:
    """An old result's response body is not what a developer should be shown
    for a defect that reproduced today."""
    with Session() as session:
        org, project = _seed(session)

        _scan(session, org, project, store, _outcome())
        first_result = session.query(models.Bug).one().result_id

        _scan(session, org, project, store, _outcome())

        assert session.query(models.Bug).one().result_id != first_result


def test_events_are_attributed_to_a_run_not_a_person(Session, store) -> None:
    """QAgent made these decisions; attributing them to whoever last logged in
    would be a lie an audit trail cannot afford."""
    with Session() as session:
        org, project = _seed(session)
        run = _scan(session, org, project, store, _outcome())

        event = session.execute(select(models.BugEvent)).scalars().first()
        assert event.actor_user_id is None
        assert event.run_id == run.id


def test_test_cases_are_linked_to_the_endpoint_they_exercise(Session, store) -> None:
    """Null endpoint_id made surface coverage impossible to compute from the
    database; nothing populated it until it was needed."""
    with Session() as session:
        org, project = _seed(session)

        result = PipelineResult(base_url="http://app", started_at=datetime.now(UTC))
        from qagent.modules.discovery.openapi import EndpointSpec

        result.endpoints = [EndpointSpec(method="GET", path="/products/{id}")]
        result.outcomes = [_outcome()]
        result.finished_at = datetime.now(UTC)

        run = models.TestRun(org_id=org.id, project_id=project.id, status=models.RunStatus.RUNNING)
        session.add(run)
        session.flush()
        persist_result(
            session,
            org_id=org.id,
            project_id=project.id,
            run=run,
            result=result,
            artifact_store=store,
        )
        session.commit()
        set_tenant(session, org.id)

        case = session.query(models.TestCase).one()
        assert case.endpoint_id is not None
