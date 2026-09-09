"""Persisting a pipeline result.

Kept separate from pipeline.py so the loop itself stays storage-agnostic: the CLI and
the eval harness run it with no database at all, while the worker writes the same
result through here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from qagent import models
from qagent.pipeline import PipelineResult

logger = logging.getLogger(__name__)


def recent_history(session: Session, org_id: UUID, project_id: UUID, limit: int = 6) -> dict[str, list[str]]:
    """Recent pass/fail sequence per case name, for flakiness detection.

    Flakiness cannot be read off a single row, which is why test_results carries a
    per-case history index.
    """
    rows = session.execute(
        select(models.TestCase.name, models.TestResult.status, models.TestResult.created_at)
        .join(models.TestResult, models.TestResult.test_case_id == models.TestCase.id)
        .join(models.TestSuite, models.TestSuite.id == models.TestCase.suite_id)
        .where(models.TestSuite.project_id == project_id, models.TestCase.org_id == org_id)
        .order_by(models.TestResult.created_at.desc())
        .limit(limit * 50)
    ).all()

    history: dict[str, list[str]] = {}
    for name, status, _ in rows:
        bucket = history.setdefault(name, [])
        if len(bucket) < limit:
            bucket.append(status.value if hasattr(status, "value") else str(status))

    # Stored newest-first; the classifier reads the tail as most recent.
    return {name: list(reversed(seq)) for name, seq in history.items()}


def _next_bug_reference(session: Session, org_id: UUID) -> str:
    count = session.query(models.Bug).filter(models.Bug.org_id == org_id).count()
    return f"BUG-{1000 + count + 1}"


def _upsert_suite(session: Session, org_id: UUID, project_id: UUID, kind: models.TestKind) -> models.TestSuite:
    suite = session.execute(
        select(models.TestSuite).where(
            models.TestSuite.project_id == project_id,
            models.TestSuite.kind == kind,
        )
    ).scalar_one_or_none()

    if suite is None:
        suite = models.TestSuite(
            org_id=org_id, project_id=project_id, name=f"Generated {kind.value}", kind=kind
        )
        session.add(suite)
        session.flush()
    return suite


def _upsert_case(
    session: Session, org_id: UUID, suite: models.TestSuite, name: str, kind: models.TestKind, spec: dict
) -> models.TestCase:
    case = session.execute(
        select(models.TestCase).where(
            models.TestCase.suite_id == suite.id, models.TestCase.name == name
        )
    ).scalar_one_or_none()

    if case is None:
        case = models.TestCase(
            org_id=org_id, suite_id=suite.id, name=name, kind=kind, spec=spec, generated_by="rule"
        )
        session.add(case)
        session.flush()
    else:
        case.spec = spec
    return case


def persist_result(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    run: models.TestRun,
    result: PipelineResult,
) -> models.TestRun:
    """Write endpoints, cases, results and bugs for one pipeline run."""
    # --- discovered endpoints ---
    for endpoint in result.endpoints:
        existing = session.execute(
            select(models.ApiEndpoint).where(
                models.ApiEndpoint.project_id == project_id,
                models.ApiEndpoint.method == endpoint.method,
                models.ApiEndpoint.path == endpoint.path,
            )
        ).scalar_one_or_none()

        if existing is None:
            session.add(
                models.ApiEndpoint(
                    org_id=org_id,
                    project_id=project_id,
                    method=endpoint.method,
                    path=endpoint.path,
                    operation_id=endpoint.operation_id,
                    summary=endpoint.summary,
                    parameters=endpoint.parameters,
                    request_schema=endpoint.request_schema,
                    responses=endpoint.responses,
                    requires_auth=endpoint.requires_auth,
                    risk_score=endpoint.risk_score,
                )
            )
        else:
            existing.risk_score = endpoint.risk_score
            existing.requires_auth = endpoint.requires_auth

    # --- cases and results ---
    for outcome in result.outcomes:
        kind = models.TestKind(outcome.kind)
        suite = _upsert_suite(session, org_id, project_id, kind)
        case = _upsert_case(session, org_id, suite, outcome.name, kind, {"request": outcome.request})

        failure_class = None
        confidence = None
        if outcome.verdict:
            try:
                failure_class = models.FailureClass(outcome.verdict["failure_class"])
            except ValueError:
                failure_class = models.FailureClass.UNKNOWN
            confidence = outcome.verdict.get("confidence")

        record = models.TestResult(
            org_id=org_id,
            run_id=run.id,
            test_case_id=case.id,
            status=models.ResultStatus(outcome.status),
            duration_ms=outcome.duration_ms,
            request=outcome.request,
            # body_text is dropped: it is unbounded and already summarised in assertions.
            response={k: v for k, v in outcome.response.items() if k != "body_text"},
            assertions=outcome.assertions,
            failure_message=outcome.failure_message,
            failure_class=failure_class,
            triage_confidence=confidence,
        )
        session.add(record)
        session.flush()

        if outcome.bug:
            bug = outcome.bug
            session.add(
                models.Bug(
                    org_id=org_id,
                    project_id=project_id,
                    result_id=record.id,
                    reference=_next_bug_reference(session, org_id),
                    title=bug.get("title", outcome.name)[:300],
                    severity=models.Severity(bug.get("severity", "medium")),
                    steps=bug.get("steps", []),
                    expected=bug.get("expected"),
                    actual=bug.get("actual"),
                    root_cause=bug.get("root_cause"),
                    suggested_fix=bug.get("suggested_fix"),
                )
            )
            session.flush()

    # --- run summary ---
    run.total = result.total
    run.passed = result.passed
    run.failed = result.failed
    run.errored = result.errored
    run.status = models.RunStatus.FAILED if (result.failed or result.errored) else models.RunStatus.PASSED
    run.finished_at = datetime.now(UTC)

    return run


def persist_agent_run(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID | None,
    run_id: UUID | None,
    agent: str,
    llm_totals: dict,
    records: list,
    trace: list | None = None,
) -> models.AgentRun:
    """Record the AI layer's activity and cost for one pipeline run."""
    agent_run = models.AgentRun(
        org_id=org_id,
        project_id=project_id,
        run_id=run_id,
        agent=agent,
        status=models.RunStatus.PASSED,
        trace=trace or [],
        total_tokens=llm_totals.get("tokens", 0),
        total_usd=llm_totals.get("usd", 0.0),
    )
    session.add(agent_run)
    session.flush()

    for record in records:
        session.add(
            models.LlmCall(
                org_id=org_id,
                agent_run_id=agent_run.id,
                provider=record.provider,
                model=record.model,
                purpose=record.purpose,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                usd=record.usd,
                latency_ms=record.latency_ms,
                ok=record.ok,
            )
        )

    return agent_run
