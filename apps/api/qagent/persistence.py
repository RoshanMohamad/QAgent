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
from qagent.modules.llm.safety import scrub
from qagent.modules.storage.local import LocalArtifactStore, store_from_settings
from qagent.modules.triage import flakiness
from qagent.pipeline import ArtifactBytes, PipelineResult

logger = logging.getLogger(__name__)


def _case_result_history(
    session: Session, case_id: UUID, window: int = flakiness.WINDOW
) -> list[str]:
    """Oldest-first pass/fail sequence for one case, for flake_rate."""
    rows = (
        session.execute(
            select(models.TestResult.status)
            .where(models.TestResult.test_case_id == case_id)
            .order_by(models.TestResult.created_at.desc())
            .limit(window)
        )
        .scalars()
        .all()
    )
    return [s.value if hasattr(s, "value") else str(s) for s in reversed(rows)]


def recent_history(
    session: Session, org_id: UUID, project_id: UUID, limit: int = 6
) -> dict[str, list[str]]:
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


def _upsert_suite(
    session: Session, org_id: UUID, project_id: UUID, kind: models.TestKind
) -> models.TestSuite:
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
    session: Session,
    org_id: UUID,
    suite: models.TestSuite,
    name: str,
    kind: models.TestKind,
    spec: dict,
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


def _persist_artifacts(
    session: Session,
    *,
    org_id: UUID,
    run_id: UUID,
    result_id: UUID,
    artifacts: list[ArtifactBytes],
    store: LocalArtifactStore,
) -> None:
    """Write evidence to storage and record the pointer (CLAUDE.md section 15).

    Text evidence (a console/page-error log) is attacker-influenced content by
    ADR-0004's own rule - it came out of a third party's browser session - so it
    is scrubbed the same way any other untrusted text is before anything
    persists it. A screenshot can't be scrubbed the same way (there is no regex
    over pixels), so it is stored as captured and `scrubbed` stays false; that
    column is what makes the difference machine-checkable rather than a claim
    in a docstring.
    """
    for artifact in artifacts:
        data = artifact.data
        scrubbed = False
        if artifact.kind == "log":
            data = scrub(data.decode("utf-8", errors="replace")).encode("utf-8")
            scrubbed = True

        stored = store.save(data, org_id=org_id, kind=artifact.kind, extension=artifact.extension)
        session.add(
            models.Artifact(
                org_id=org_id,
                run_id=run_id,
                result_id=result_id,
                kind=artifact.kind,
                storage_key=stored.storage_key,
                content_type=artifact.content_type,
                size_bytes=stored.size_bytes,
                scrubbed=scrubbed,
            )
        )


def persist_result(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    run: models.TestRun,
    result: PipelineResult,
    artifact_store: LocalArtifactStore | None = None,
) -> models.TestRun:
    """Write endpoints, cases, results, bugs and evidence for one pipeline run."""
    store = artifact_store or store_from_settings()

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
        case = _upsert_case(
            session, org_id, suite, outcome.name, kind, {"request": outcome.request}
        )

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

        # Updated after every run, pass or fail: a case that is unreliable but
        # happens to pass this time must not look healthy just because this one
        # result was green.
        case_history = _case_result_history(session, case.id)
        case.flake_rate = flakiness.compute_flake_rate(case_history)
        case.quarantined = flakiness.should_quarantine(case_history, case.flake_rate)

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

            if outcome.artifacts:
                _persist_artifacts(
                    session,
                    org_id=org_id,
                    run_id=run.id,
                    result_id=record.id,
                    artifacts=outcome.artifacts,
                    store=store,
                )

    # --- run summary ---
    run.total = result.total
    run.passed = result.passed
    run.failed = result.failed
    run.errored = result.errored
    run.status = (
        models.RunStatus.FAILED if (result.failed or result.errored) else models.RunStatus.PASSED
    )
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


def persist_security_findings(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    findings: list,
    tool: str = "semgrep",
) -> int:
    """Insert findings new to this project, identified by rule+path+line.

    Re-scanning a repository reports the same unfixed issues every time; without
    this dedupe, the count would grow every run instead of reflecting what's
    actually open. Returns the number of genuinely new findings inserted.
    """
    existing = {
        (rule_id, path, line)
        for rule_id, path, line in session.execute(
            select(
                models.SecurityFinding.rule_id,
                models.SecurityFinding.path,
                models.SecurityFinding.line,
            ).where(
                models.SecurityFinding.project_id == project_id,
                models.SecurityFinding.tool == tool,
            )
        ).all()
    }

    inserted = 0
    for finding in findings:
        if finding.dedupe_key() in existing:
            continue
        session.add(
            models.SecurityFinding(
                org_id=org_id,
                project_id=project_id,
                tool=tool,
                rule_id=finding.rule_id,
                path=finding.path,
                line=finding.line,
                title=finding.title[:300],
                severity=models.Severity(finding.severity),
                message=finding.message,
                confidence=finding.confidence,
                cwe=finding.cwe,
                owasp=finding.owasp,
            )
        )
        inserted += 1

    return inserted


def persist_performance_runs(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    base_url: str,
    scenarios: list,
    max_failed_rate: float,
    max_p95_ms: float,
    tool: str = "k6",
) -> list[models.PerformanceRun]:
    """Insert one row per VU-level scenario. Unlike security findings, these are
    never deduped: each scan is a new data point in a project's performance
    history, not a persistent state to converge on."""
    rows = []
    for scenario in scenarios:
        row = models.PerformanceRun(
            org_id=org_id,
            project_id=project_id,
            tool=tool,
            base_url=base_url,
            vus=scenario.vus,
            duration_s=scenario.duration_s,
            requests=scenario.requests,
            requests_per_s=scenario.requests_per_s,
            failed_rate=scenario.failed_rate,
            latency_avg_ms=scenario.latency_avg_ms,
            latency_p95_ms=scenario.latency_p95_ms,
            latency_p99_ms=scenario.latency_p99_ms,
            latency_max_ms=scenario.latency_max_ms,
            passed=scenario.passes(max_failed_rate=max_failed_rate, max_p95_ms=max_p95_ms),
        )
        session.add(row)
        rows.append(row)

    return rows
