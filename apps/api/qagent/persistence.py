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
from qagent.modules.observability import metrics
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
    endpoint: models.ApiEndpoint | None = None,
) -> models.TestCase:
    case = session.execute(
        select(models.TestCase).where(
            models.TestCase.suite_id == suite.id, models.TestCase.name == name
        )
    ).scalar_one_or_none()

    if case is None:
        case = models.TestCase(
            org_id=org_id,
            suite_id=suite.id,
            name=name,
            kind=kind,
            spec=spec,
            generated_by="rule",
            endpoint_id=endpoint.id if endpoint is not None else None,
        )
        session.add(case)
        session.flush()
    else:
        case.spec = spec
        # Backfilled on re-scan: cases created before this link existed have a
        # null endpoint_id, and leaving them null would under-report coverage
        # forever on any project that has already been scanned once.
        if endpoint is not None and case.endpoint_id is None:
            case.endpoint_id = endpoint.id
    return case


def _existing_bug(
    session: Session, project_id: UUID, case_id: UUID
) -> models.Bug | None:
    """The open-or-closed defect this test case has already produced.

    Identity is the *test case*, not the bug title: a title is written by a
    model when one is configured and is not stable between runs, so matching on
    it would file the same defect twice with two references. The case is
    deterministic - it comes from one rule applied to one endpoint - which
    makes it the right key.
    """
    return session.execute(
        select(models.Bug)
        .join(models.TestResult, models.TestResult.id == models.Bug.result_id)
        .where(
            models.Bug.project_id == project_id,
            models.TestResult.test_case_id == case_id,
        )
        .order_by(models.Bug.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _record_event(
    session: Session,
    *,
    bug: models.Bug,
    event: str,
    run_id: UUID | None = None,
    from_value: str | None = None,
    to_value: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one immutable entry to a defect's history.

    `actor_user_id` is deliberately left null: every event written from here is
    QAgent's own decision during a run, and attributing it to a person would be
    a lie that an audit trail cannot afford.
    """
    session.add(
        models.BugEvent(
            org_id=bug.org_id,
            bug_id=bug.id,
            event=event,
            from_value=from_value,
            to_value=to_value,
            run_id=run_id,
            detail=detail or {},
        )
    )


def _record_code_evidence(
    session: Session, *, bug: models.Bug, report: dict, is_new: bool
) -> None:
    """Persist the retrieved "affected code" as a machine-authored comment.

    Repository RAG (modules/rag/) works out which function explains a defect,
    and until now that answer only ever reached the terminal - nothing wrote it
    down, so the dashboard and the API never saw it. A generated comment is the
    right home: it is attributable (``generated=True`` distinguishes a
    machine's opinion from a colleague's), it lives alongside the human
    discussion, and it does not pretend to be part of the defect's definition.

    Written once per distinct location rather than on every run, because a
    defect that reproduces fifty times should not accumulate fifty identical
    comments.
    """
    location = report.get("affected_location")
    if not location:
        return

    if not is_new:
        already = session.execute(
            select(models.BugComment).where(
                models.BugComment.bug_id == bug.id,
                models.BugComment.generated.is_(True),
                models.BugComment.body.contains(str(location)),
            )
        ).first()
        if already:
            return

    others = [
        entry.get("location")
        for entry in (report.get("affected_code") or [])
        if entry.get("location") and entry.get("location") != location
    ]

    body = f"Likely cause: `{location}`"
    if report.get("root_cause"):
        body += f"\n\n{report['root_cause']}"
    if others:
        body += "\n\nAlso retrieved: " + ", ".join(f"`{o}`" for o in others[:3])

    session.add(
        models.BugComment(
            org_id=bug.org_id,
            bug_id=bug.id,
            # No author: QAgent wrote this, and attributing it to whoever last
            # logged in would misrepresent where the claim came from.
            author_user_id=None,
            body=body,
            generated=True,
        )
    )


def _upsert_bug(
    session: Session,
    *,
    org_id: UUID,
    project_id: UUID,
    run: models.TestRun,
    case: models.TestCase,
    result: models.TestResult,
    report: dict,
    fallback_title: str,
) -> models.Bug:
    """Create the defect, or update the one this case already produced.

    Before `bug_events` existed this unconditionally inserted, so a defect that
    survived ten runs became ten `BUG-` references and the dashboard counted it
    ten times. Now a repeat is an update plus a history entry, which is both the
    honest count and the thing that makes "how long has this been open" and
    "did it regress after we closed it" answerable at all.
    """
    severity = models.Severity(str(report.get("severity", "medium")))
    title = str(report.get("title") or fallback_title)[:300]
    existing = _existing_bug(session, project_id, case.id)

    if existing is None:
        bug = models.Bug(
            org_id=org_id,
            project_id=project_id,
            result_id=result.id,
            reference=_next_bug_reference(session, org_id),
            title=title,
            severity=severity,
            steps=report.get("steps", []),
            expected=report.get("expected"),
            actual=report.get("actual"),
            root_cause=report.get("root_cause"),
            suggested_fix=report.get("suggested_fix"),
        )
        session.add(bug)
        session.flush()
        _record_event(
            session, bug=bug, event="opened", run_id=run.id, to_value=severity.value
        )
        _record_code_evidence(session, bug=bug, report=report, is_new=True)
        metrics.DEFECTS_TOTAL.labels(severity=severity.value).inc()
        return bug

    was_status = existing.status
    was_severity = existing.severity

    # Point at the newest evidence: an old result row's response body is not
    # what a developer should be shown for a defect that reproduced today.
    existing.result_id = result.id
    existing.title = title
    existing.steps = report.get("steps", [])
    existing.expected = report.get("expected")
    existing.actual = report.get("actual")
    existing.root_cause = report.get("root_cause")
    existing.suggested_fix = report.get("suggested_fix")
    existing.severity = severity

    if was_status != "open":
        # A defect that comes back after being closed is the single most
        # important thing this table records: it is a regression, and the
        # status column alone would simply flip back to "open" and forget.
        existing.status = "open"
        _record_event(
            session,
            bug=existing,
            event="reopened",
            run_id=run.id,
            from_value=was_status,
            to_value="open",
        )
        metrics.DEFECTS_TOTAL.labels(severity=severity.value).inc()
    elif was_severity != severity:
        _record_event(
            session,
            bug=existing,
            event="severity_changed",
            run_id=run.id,
            from_value=was_severity.value,
            to_value=severity.value,
        )
    else:
        _record_event(session, bug=existing, event="reproduced", run_id=run.id)

    # Re-checked on every run, not just on open: if the code moved, the
    # retrieved location changed, and the old comment now points at a line
    # that means something else.
    _record_code_evidence(session, bug=existing, report=report, is_new=False)

    session.flush()
    return existing


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

    # --- plan and coverage ---
    if result.plan is not None:
        run.plan = result.plan.to_dict()
    if result.coverage is not None:
        run.coverage = result.coverage.to_dict()

    # --- discovered endpoints ---
    # Keyed by "METHOD /path" so each case can be linked back to the endpoint it
    # exercises. Without that link `TestCase.endpoint_id` stays null - as it did
    # until surface coverage needed it - and there is no way to ask the database
    # which endpoints nobody ever tested.
    endpoint_rows: dict[str, models.ApiEndpoint] = {}

    for endpoint in result.endpoints:
        existing = session.execute(
            select(models.ApiEndpoint).where(
                models.ApiEndpoint.project_id == project_id,
                models.ApiEndpoint.method == endpoint.method,
                models.ApiEndpoint.path == endpoint.path,
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = models.ApiEndpoint(
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
            session.add(existing)
        else:
            existing.risk_score = endpoint.risk_score
            existing.requires_auth = endpoint.requires_auth

        endpoint_rows[endpoint.key()] = existing

    # Flushed so the new rows have ids to link cases against.
    session.flush()

    # --- cases and results ---
    for outcome in result.outcomes:
        kind = models.TestKind(outcome.kind)
        suite = _upsert_suite(session, org_id, project_id, kind)
        case = _upsert_case(
            session,
            org_id,
            suite,
            outcome.name,
            kind,
            {"request": outcome.request},
            endpoint=endpoint_rows.get(outcome.endpoint_key or ""),
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
            _upsert_bug(
                session,
                org_id=org_id,
                project_id=project_id,
                run=run,
                case=case,
                result=record,
                report=outcome.bug,
                fallback_title=outcome.name,
            )

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
    metrics.RUNS_TOTAL.labels(status=run.status.value).inc()

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
    metrics.LLM_SPEND_USD_TOTAL.inc(llm_totals.get("usd", 0.0))

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
